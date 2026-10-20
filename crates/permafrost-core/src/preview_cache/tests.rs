use super::*;
use crate::{fs, model::now};
use md5::{Digest, Md5};
use serde_json::json;
use wiremock::{
    matchers::{method, path, query_param},
    Mock, MockServer, ResponseTemplate,
};

async fn fixture(
    bytes: &[u8],
    corrupt: bool,
) -> (MockServer, Drive, tempfile::TempDir, Project, Entry) {
    let server = MockServer::start().await;
    let root = tempfile::tempdir().unwrap();
    let project = Project {
        id: "p".into(),
        name: "P".into(),
        local_root: root.path().into(),
        drive_folder_id: "root".into(),
        read_only: false,
    };
    let expected = Entry {
        path: "assembly.step".into(),
        size: bytes.len() as u64,
        md5: format!("{:x}", Md5::digest(bytes)),
        modified: "1".into(),
        remote_id: Some("file".into()),
        version: Some("1".into()),
        modified_by: None,
    };
    Mock::given(method("GET")).and(path("/files/file")).and(query_param("fields", "id,name,mimeType,md5Checksum,size,modifiedTime,version,parents,trashed,lastModifyingUser(displayName,emailAddress)"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id":"file","name":"assembly.step","size":bytes.len().to_string(),
            "md5Checksum":expected.md5,"modifiedTime":"1","version":"1","parents":["root"]
        }))).mount(&server).await;
    let body = if corrupt {
        vec![b'x'; bytes.len()]
    } else {
        bytes.to_vec()
    };
    Mock::given(method("GET"))
        .and(path("/files/file"))
        .and(query_param("alt", "media"))
        .respond_with(ResponseTemplate::new(200).set_body_bytes(body))
        .expect(1)
        .mount(&server)
        .await;
    let drive = Drive::fixture(server.uri());
    (server, drive, root, project, expected)
}

#[tokio::test]
async fn previews_use_hidden_verified_content_without_touching_real_files() {
    let (_server, drive, root, project, expected) = fixture(b"STEP contents", false).await;
    std::fs::write(root.path().join("assembly.step"), b"unsynced local edit").unwrap();
    let cancel = CancellationToken::new();
    let cached = fetch(&drive, &project, &expected, "root", &cancel)
        .await
        .unwrap();
    assert!(cached.starts_with(
        root.path()
            .canonicalize()
            .unwrap()
            .join(".permafrost/preview-cache")
    ));
    assert_eq!(std::fs::read(&cached).unwrap(), b"STEP contents");
    assert_eq!(
        std::fs::read(root.path().join("assembly.step")).unwrap(),
        b"unsynced local edit"
    );
    assert_eq!(
        fetch(&drive, &project, &expected, "root", &cancel)
            .await
            .unwrap(),
        cached
    );
    assert_eq!(fs::scan(root.path(), &cancel).unwrap().len(), 1);
    let mut next_version = expected.clone();
    next_version.version = Some("2".into());
    assert!(take(root.path(), &next_version, &cancel).unwrap().is_none());
    let staged = take(root.path(), &expected, &cancel).unwrap().unwrap();
    assert_eq!(std::fs::read(staged.path()).unwrap(), b"STEP contents");
    drop(staged);
    assert!(!cached.exists());
}

#[tokio::test]
async fn corrupt_preview_downloads_are_never_reused() {
    let (_server, drive, root, project, expected) = fixture(b"STEP contents", true).await;
    let cancel = CancellationToken::new();
    assert!(fetch(&drive, &project, &expected, "root", &cancel)
        .await
        .is_err());
    assert!(take(root.path(), &expected, &cancel).unwrap().is_none());
    assert!(!root.path().join("assembly.step").exists());
}

#[tokio::test]
async fn purge_expires_cache_only_and_leaves_fresh_entries_and_backups() {
    let (_server, drive, root, project, expected) = fixture(b"STEP contents", false).await;
    let cached = fetch(
        &drive,
        &project,
        &expected,
        "root",
        &CancellationToken::new(),
    )
    .await
    .unwrap();
    std::fs::create_dir_all(root.path().join(".permafrost/backups")).unwrap();
    std::fs::write(
        root.path().join(".permafrost/backups/saved.step"),
        b"backup",
    )
    .unwrap();
    std::fs::write(root.path().join("real.step"), b"real").unwrap();
    assert_eq!(purge(root.path(), now()).unwrap(), 0);
    assert!(cached.exists());
    assert!(purge(root.path(), now() + DAY + 1).unwrap() >= 2);
    assert!(!cached.exists());
    assert!(root.path().join("real.step").exists());
    assert!(root.path().join(".permafrost/backups/saved.step").exists());
}

#[tokio::test]
async fn cancellation_and_oversize_fail_before_downloading() {
    let server = MockServer::start().await;
    let drive = Drive::fixture(server.uri());
    let root = tempfile::tempdir().unwrap();
    let project = Project {
        id: "p".into(),
        name: "P".into(),
        local_root: root.path().into(),
        drive_folder_id: "root".into(),
        read_only: false,
    };
    let mut expected = Entry {
        path: "model.step".into(),
        size: MAX_BYTES + 1,
        md5: "abc".into(),
        modified: "1".into(),
        remote_id: Some("f".into()),
        version: Some("1".into()),
        modified_by: None,
    };
    let error = fetch(
        &drive,
        &project,
        &expected,
        "root",
        &CancellationToken::new(),
    )
    .await
    .unwrap_err();
    assert_eq!(error.to_string(), LIMIT_ERROR);
    expected.size = 1;
    let cancel = CancellationToken::new();
    cancel.cancel();
    assert!(matches!(
        fetch(&drive, &project, &expected, "root", &cancel).await,
        Err(Error::Cancelled)
    ));
    assert!(server.received_requests().await.unwrap().is_empty());
}

#[cfg(unix)]
#[test]
fn cache_cleanup_does_not_follow_symlinks() {
    let root = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    std::fs::write(outside.path().join("important.step"), b"keep").unwrap();
    std::fs::create_dir(root.path().join(".permafrost")).unwrap();
    std::os::unix::fs::symlink(
        outside.path(),
        root.path().join(".permafrost/preview-cache"),
    )
    .unwrap();
    assert!(purge(root.path(), now() + DAY + 1).is_err());
    assert!(outside.path().join("important.step").exists());
}
