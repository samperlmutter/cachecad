//! Exercise the actual scanner, planner, HTTP transfers, journal and baselines.
use crate::{drive::Drive, fs, model::*, planner, store::Store, transfer, Error};
use md5::{Digest, Md5};
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
use tokio_util::sync::CancellationToken;
use wiremock::{Mock, MockServer, Request, Respond, ResponseTemplate};

#[derive(Default)]
struct Cloud {
    bytes: Option<Vec<u8>>,
    version: usize,
    trashed: bool,
    corrupt_download: bool,
    fail_after_upload: bool,
    mutations: usize,
    pending: Vec<u8>,
    chunks: usize,
}
impl Cloud {
    fn set(&mut self, bytes: &[u8]) {
        self.bytes = Some(bytes.to_vec());
        self.version += 1;
        self.trashed = false;
    }
    fn metadata(&self) -> Value {
        let bytes = self.bytes.as_deref().unwrap_or_default();
        json!({"id":"part","name":"part.sldprt","mimeType":"application/octet-stream","md5Checksum":format!("{:x}", Md5::digest(bytes)),"size":bytes.len().to_string(),"version":self.version.to_string(),"modifiedTime":"2026-01-01T00:00:00Z","parents":["root"],"trashed":self.trashed})
    }
}
struct Provider {
    state: Arc<Mutex<Cloud>>,
    endpoint: String,
}
impl Respond for Provider {
    fn respond(&self, request: &Request) -> ResponseTemplate {
        assert_eq!(
            request.headers.get("authorization").unwrap(),
            "Bearer synthetic"
        );
        let mut state = self.state.lock().unwrap();
        let query: std::collections::HashMap<_, _> = request.url.query_pairs().collect();
        match (request.method.as_str(), request.url.path()) {
            ("GET", "/files/root") => ResponseTemplate::new(200).set_body_json(
                json!({"id":"root","name":"QA","mimeType":"application/vnd.google-apps.folder"}),
            ),
            ("GET", "/files") => {
                assert_eq!(query.get("includeItemsFromAllDrives").unwrap(), "true");
                let files = if state.bytes.is_some() && !state.trashed {
                    vec![state.metadata()]
                } else {
                    vec![]
                };
                ResponseTemplate::new(200).set_body_json(json!({"files":files}))
            }
            ("GET", "/files/part") if query.get("alt").is_some_and(|v| v == "media") => {
                let mut bytes = state.bytes.clone().unwrap();
                if state.corrupt_download {
                    bytes[0] ^= 1;
                }
                ResponseTemplate::new(200).set_body_bytes(bytes)
            }
            ("GET", "/files/part") => ResponseTemplate::new(200)
                .insert_header("etag", format!("\"{}\"", state.version))
                .set_body_json(state.metadata()),
            ("POST", "/files") | ("PATCH", "/files/part") if query.contains_key("uploadType") => {
                assert_eq!(query.get("uploadType").unwrap(), "resumable");
                if request.method.as_str() == "PATCH" {
                    assert_eq!(
                        request.headers.get("if-match").unwrap().to_str().unwrap(),
                        format!("\"{}\"", state.version)
                    );
                }
                state.mutations += 1;
                state.pending.clear();
                ResponseTemplate::new(200)
                    .insert_header("location", format!("{}/session", self.endpoint))
            }
            ("PUT", "/session") => {
                state.chunks += 1;
                state.pending.extend_from_slice(&request.body);
                let range = request
                    .headers
                    .get("content-range")
                    .unwrap()
                    .to_str()
                    .unwrap();
                let total: usize = range.rsplit('/').next().unwrap().parse().unwrap();
                if state.pending.len() < total {
                    return ResponseTemplate::new(308)
                        .insert_header("range", format!("bytes=0-{}", state.pending.len() - 1));
                }
                assert_eq!(state.pending.len(), total);
                let bytes = std::mem::take(&mut state.pending);
                state.set(&bytes);
                if state.fail_after_upload {
                    ResponseTemplate::new(503)
                } else {
                    ResponseTemplate::new(200).set_body_json(state.metadata())
                }
            }
            ("PATCH", "/files/part") => {
                let body: Value = serde_json::from_slice(&request.body).unwrap();
                assert_eq!(body, json!({"trashed":true}));
                state.mutations += 1;
                state.trashed = true;
                ResponseTemplate::new(200).set_body_json(state.metadata())
            }
            _ => ResponseTemplate::new(404),
        }
    }
}
struct Fixture {
    _server: MockServer,
    root: tempfile::TempDir,
    state: Arc<Mutex<Cloud>>,
    drive: Drive,
    store: Mutex<Store>,
    project: Project,
    cancel: CancellationToken,
}
impl Fixture {
    async fn new() -> Self {
        let server = MockServer::start().await;
        let state = Arc::new(Mutex::new(Cloud::default()));
        Mock::given(|_: &Request| true)
            .respond_with(Provider {
                state: state.clone(),
                endpoint: server.uri(),
            })
            .mount(&server)
            .await;
        let root = tempfile::tempdir().unwrap();
        let project = Project {
            id: "p".into(),
            name: "QA".into(),
            local_root: root.path().into(),
            drive_folder_id: "root".into(),
            read_only: false,
        };
        let mut store = Store::memory().unwrap();
        store.save_project(&project).unwrap();
        Self {
            drive: Drive::fixture(server.uri()),
            _server: server,
            root,
            state,
            store: Mutex::new(store),
            project,
            cancel: CancellationToken::new(),
        }
    }
    fn local(&self, bytes: &[u8]) {
        std::fs::write(self.root.path().join("part.sldprt"), bytes).unwrap();
    }
    async fn plan(&self) -> Plan {
        let local = fs::scan(self.root.path(), &self.cancel).unwrap();
        let (remote, folders) = self.drive.scan("root", &self.cancel).await.unwrap();
        let mut store = self.store.lock().unwrap();
        store.record_equal("p", &local, &remote).unwrap();
        planner::plan(
            &self.project,
            local,
            remote,
            &store.baseline("p").unwrap(),
            folders,
        )
    }
    async fn apply(&self, plan: &Plan, kind: ActionKind) -> crate::Result<transfer::Report> {
        let ids = vec![plan
            .actions
            .iter()
            .find(|a| a.kind == kind)
            .expect("Expected sync direction")
            .id
            .clone()];
        transfer::execute(
            &self.store,
            &self.drive,
            &self.project,
            plan,
            transfer::Selection {
                ids: &ids,
                consent: true,
            },
            &self.cancel,
            &|_| {},
        )
        .await
    }
    async fn sync(&self, kind: ActionKind) {
        let plan = self.plan().await;
        let report = self.apply(&plan, kind).await.unwrap();
        assert_eq!(
            (report.completed, report.failed, report.cancelled),
            (1, 0, false)
        );
        let latest = self.plan().await;
        assert!(latest.actions.is_empty(), "{:?}", latest.actions);
    }
}

#[tokio::test]
async fn binary_upload_download_revisions_conflicts_and_both_deletions() {
    let f = Fixture::new().await;
    let native_bytes = b"\0solidworks\xff\xfe\0binary";
    f.local(native_bytes);
    f.sync(ActionKind::UploadNew).await;
    assert_eq!(
        f.state.lock().unwrap().bytes.as_deref(),
        Some(native_bytes.as_slice())
    );
    f.local(b"local revision");
    f.sync(ActionKind::UploadRevision).await;
    f.state.lock().unwrap().set(b"remote revision");
    f.sync(ActionKind::DownloadRevision).await;
    f.local(b"conflict local");
    f.state.lock().unwrap().set(b"conflict remote");
    f.sync(ActionKind::UploadConflict).await;
    f.local(b"second local");
    f.state.lock().unwrap().set(b"second remote");
    f.sync(ActionKind::DownloadConflict).await;
    assert_eq!(
        std::fs::read(f.root.path().join("part.sldprt")).unwrap(),
        b"second remote"
    );
    f.state.lock().unwrap().trashed = true;
    f.sync(ActionKind::DeleteLocal).await;
    assert!(!f.root.path().join("part.sldprt").exists());
    assert!(f.root.path().join(".permafrost/backups").is_dir());
    f.state.lock().unwrap().set(native_bytes);
    f.sync(ActionKind::DownloadNew).await;
    assert_eq!(
        std::fs::read(f.root.path().join("part.sldprt")).unwrap(),
        native_bytes
    );
    std::fs::remove_file(f.root.path().join("part.sldprt")).unwrap();
    f.sync(ActionKind::DeleteCloud).await;
    assert!(f.state.lock().unwrap().trashed);
    assert_eq!(f.store.lock().unwrap().activity().unwrap().len(), 8);
}

#[tokio::test]
async fn large_binary_upload_continues_after_resumable_chunk_acknowledgement() {
    let f = Fixture::new().await;
    let bytes = vec![0xab; 8 * 1024 * 1024 + 257];
    f.local(&bytes);
    f.sync(ActionKind::UploadNew).await;
    let state = f.state.lock().unwrap();
    assert_eq!(state.chunks, 2);
    assert_eq!(state.bytes.as_ref().unwrap(), &bytes);
}

#[tokio::test]
async fn corruption_and_stale_plans_preserve_the_local_copy() {
    let f = Fixture::new().await;
    f.local(b"original");
    f.sync(ActionKind::UploadNew).await;
    f.state.lock().unwrap().set(b"new remote");
    let plan = f.plan().await;
    f.local(b"changed while reviewing");
    assert!(matches!(
        f.apply(&plan, ActionKind::DownloadRevision).await,
        Err(Error::Stale)
    ));
    let plan = f.plan().await;
    f.state.lock().unwrap().corrupt_download = true;
    let report = f.apply(&plan, ActionKind::DownloadConflict).await.unwrap();
    assert_eq!((report.completed, report.failed), (0, 1));
    assert_eq!(
        std::fs::read(f.root.path().join("part.sldprt")).unwrap(),
        b"changed while reviewing"
    );
    assert!(!f.store.lock().unwrap().has_unresolved("p").unwrap());
}

#[tokio::test]
async fn uncertain_write_blocks_replay_and_read_only_blocks_uploads() {
    let mut f = Fixture::new().await;
    f.local(b"original");
    let plan = f.plan().await;
    f.project.read_only = true;
    assert!(f.apply(&plan, ActionKind::UploadNew).await.is_err());
    assert_eq!(f.state.lock().unwrap().mutations, 0);
    f.project.read_only = false;
    f.state.lock().unwrap().fail_after_upload = true;
    let report = f.apply(&plan, ActionKind::UploadNew).await.unwrap();
    assert_eq!((report.completed, report.failed), (0, 1));
    assert!(f.store.lock().unwrap().has_unresolved("p").unwrap());
    assert!(matches!(
        f.apply(&plan, ActionKind::UploadNew).await,
        Err(Error::RecoveryRequired)
    ));
    assert_eq!(f.state.lock().unwrap().mutations, 1);
}
