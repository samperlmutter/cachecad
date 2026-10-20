use crate::{
    auth::checked,
    fs::{identity, validate_relative},
    model::*,
    Error, Result,
};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    path::Path,
    time::Duration,
};
use tokio::{
    fs::File,
    io::{AsyncReadExt, AsyncWriteExt},
};
use tokio_util::sync::CancellationToken;

const FOLDER: &str = "application/vnd.google-apps.folder";
const FIELDS: &str = "id,name,mimeType,md5Checksum,size,modifiedTime,version,parents,trashed,lastModifyingUser(displayName,emailAddress)";
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DriveFile {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub mime_type: String,
    pub md5_checksum: Option<String>,
    pub size: Option<String>,
    pub modified_time: Option<String>,
    pub version: Option<String>,
    #[serde(default)]
    pub parents: Vec<String>,
    #[serde(default)]
    pub trashed: bool,
    pub last_modifying_user: Option<DriveUser>,
    #[serde(skip)]
    pub etag: Option<String>,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DriveUser {
    pub display_name: Option<String>,
    pub email_address: Option<String>,
}
impl DriveFile {
    pub fn entry(&self, path: String) -> Result<Entry> {
        Ok(Entry {
            path,
            size: self
                .size
                .as_ref()
                .and_then(|s| s.parse().ok())
                .ok_or_else(incomplete)?,
            md5: self.md5_checksum.clone().ok_or_else(incomplete)?,
            modified: self.modified_time.clone().ok_or_else(incomplete)?,
            remote_id: Some(self.id.clone()),
            version: Some(self.version.clone().ok_or_else(incomplete)?),
            modified_by: self
                .last_modifying_user
                .as_ref()
                .and_then(|u| u.display_name.clone().or_else(|| u.email_address.clone())),
        })
    }
    pub fn matches(&self, expected: &Entry, parent: &str) -> bool {
        !self.trashed
            && self.remote_id_matches(expected)
            && self.name == expected.path.rsplit('/').next().unwrap_or("")
            && self.parents.iter().any(|p| p == parent)
            && self.md5_checksum.as_ref() == Some(&expected.md5)
            && self.version == expected.version
            && self.size.as_ref().and_then(|s| s.parse::<u64>().ok()) == Some(expected.size)
    }
    fn remote_id_matches(&self, expected: &Entry) -> bool {
        expected.remote_id.as_ref() == Some(&self.id)
    }
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Listing {
    #[serde(default)]
    files: Vec<DriveFile>,
    next_page_token: Option<String>,
    #[serde(default)]
    incomplete_search: bool,
}
fn incomplete() -> Error {
    Error::Invalid("Google Drive returned an incomplete or unsupported project listing. No changes were planned.".into())
}

pub struct Upload<'a> {
    pub source: &'a Path,
    pub size: u64,
    pub name: &'a str,
    pub parent: &'a str,
    pub existing: Option<&'a DriveFile>,
    pub keep_forever: bool,
}

#[derive(Clone)]
pub struct Drive {
    client: reqwest::Client,
    token: String,
    base: String,
    upload_base: String,
}
impl Drive {
    pub fn new(token: String) -> Result<Self> {
        Self::with_endpoints(
            token,
            "https://www.googleapis.com/drive/v3".into(),
            "https://www.googleapis.com/upload/drive/v3".into(),
        )
    }
    fn with_endpoints(token: String, base: String, upload_base: String) -> Result<Self> {
        Ok(Self {
            token,
            base,
            upload_base,
            client: reqwest::Client::builder()
                .connect_timeout(Duration::from_secs(15))
                .timeout(Duration::from_secs(120))
                .redirect(reqwest::redirect::Policy::none())
                .build()?,
        })
    }
    #[cfg(test)]
    pub(crate) fn fixture(endpoint: String) -> Self {
        Self::with_endpoints("synthetic".into(), endpoint.clone(), endpoint).unwrap()
    }
    fn url(&self, id: &str) -> Result<String> {
        if id.is_empty()
            || !id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        {
            return Err(Error::Invalid("Invalid Google Drive file ID.".into()));
        }
        Ok(format!("{}/files/{id}", self.base))
    }
    pub async fn metadata(&self, id: &str) -> Result<Option<DriveFile>> {
        let response = self
            .client
            .get(self.url(id)?)
            .bearer_auth(&self.token)
            .query(&[("fields", FIELDS), ("supportsAllDrives", "true")])
            .send()
            .await?;
        if response.status().as_u16() == 404 {
            return Ok(None);
        }
        let response = checked(response)?;
        let etag = response
            .headers()
            .get(reqwest::header::ETAG)
            .and_then(|s| s.to_str().ok())
            .map(String::from);
        let mut file: DriveFile = response.json().await?;
        file.etag = etag;
        Ok(Some(file))
    }
    pub async fn children(
        &self,
        parent: &str,
        cancel: &CancellationToken,
    ) -> Result<Vec<DriveFile>> {
        self.url(parent)?;
        let query = format!("'{parent}' in parents and trashed = false");
        let fields = format!("nextPageToken,incompleteSearch,files({FIELDS})");
        let mut page = String::new();
        let mut pages = BTreeSet::new();
        let mut files = Vec::new();
        loop {
            if cancel.is_cancelled() {
                return Err(Error::Cancelled);
            }
            let response = tokio::select! {
                _ = cancel.cancelled() => return Err(Error::Cancelled),
                r = self.client.get(format!("{}/files",self.base)).bearer_auth(&self.token).query(&[("q",query.as_str()),("fields",fields.as_str()),("pageSize","1000"),("pageToken",&page),("supportsAllDrives","true"),("includeItemsFromAllDrives","true")]).send() => checked(r?)?,
            };
            let listing: Listing = response.json().await?;
            if listing.incomplete_search {
                return Err(incomplete());
            }
            files.extend(listing.files);
            if files.len() > 1_000_000 {
                return Err(Error::Invalid(
                    "This folder exceeds the one-million-item scan limit.".into(),
                ));
            }
            match listing.next_page_token {
                Some(next) if !next.is_empty() && pages.insert(next.clone()) => page = next,
                Some(_) => return Err(incomplete()),
                None => break,
            }
        }
        Ok(files)
    }
    pub async fn scan(
        &self,
        root: &str,
        cancel: &CancellationToken,
    ) -> Result<(Tree, BTreeMap<String, String>)> {
        tokio::select! {
            biased;
            _ = cancel.cancelled() => Err(Error::Cancelled),
            result = self.scan_inner(root, cancel) => result,
        }
    }
    async fn scan_inner(
        &self,
        root: &str,
        cancel: &CancellationToken,
    ) -> Result<(Tree, BTreeMap<String, String>)> {
        let root_file = self.metadata(root).await?.ok_or_else(incomplete)?;
        if root_file.trashed || root_file.mime_type != FOLDER {
            return Err(Error::Invalid(
                "The connected Drive folder is unavailable or is not a folder.".into(),
            ));
        }
        let mut queue = VecDeque::from([(String::new(), root.to_string(), 0usize)]);
        let mut folders = BTreeMap::from([(String::new(), root.into())]);
        let mut identities = BTreeSet::new();
        let mut visited = BTreeSet::from([root.to_string()]);
        let mut tree = Tree::new();
        while let Some((path, id, depth)) = queue.pop_front() {
            if depth > 128 {
                return Err(incomplete());
            }
            for file in self.children(&id, cancel).await? {
                let full = if path.is_empty() {
                    file.name.clone()
                } else {
                    format!("{path}/{}", file.name)
                };
                validate_relative(&full)?;
                if file.trashed
                    || !file.parents.contains(&id)
                    || !identities.insert(identity(&full))
                    || !visited.insert(file.id.clone())
                {
                    return Err(Error::Invalid("Drive contains duplicate or colliding filenames. Resolve them before syncing.".into()));
                }
                if crate::fs::ignored(&file.name) {
                    continue;
                }
                if file.mime_type == FOLDER {
                    folders.insert(full.clone(), file.id.clone());
                    queue.push_back((full, file.id, depth + 1));
                } else {
                    if file.mime_type.starts_with("application/vnd.google-apps.") {
                        return Err(Error::Invalid("This folder contains a Google document or shortcut that cannot be mirrored as a binary file. Move it outside the CAD project.".into()));
                    }
                    tree.insert(full.clone(), file.entry(full)?);
                }
            }
        }
        Ok((tree, folders))
    }
    pub async fn create_folder(&self, name: &str, parent: &str) -> Result<String> {
        let response = checked(
            self.client
                .post(format!("{}/files", self.base))
                .bearer_auth(&self.token)
                .query(&[("supportsAllDrives", "true"), ("fields", "id")])
                .json(&serde_json::json!({"name":name,"mimeType":FOLDER,"parents":[parent]}))
                .send()
                .await?,
        )?;
        let data: serde_json::Value = response.json().await?;
        data["id"].as_str().map(String::from).ok_or_else(incomplete)
    }
    pub async fn trash(&self, file: &DriveFile) -> Result<()> {
        let mut request = self
            .client
            .patch(self.url(&file.id)?)
            .bearer_auth(&self.token)
            .query(&[("supportsAllDrives", "true")])
            .json(&serde_json::json!({"trashed":true}));
        // If Google provides an ETag, enforce it in addition to the observed
        // version checks. Version-only APIs retain a check/write race.
        if let Some(etag) = &file.etag {
            request = request.header(reqwest::header::IF_MATCH, etag);
        }
        checked(request.send().await?)?;
        if self.metadata(&file.id).await?.is_some_and(|f| !f.trashed) {
            return Err(Error::Stale);
        }
        Ok(())
    }
    pub async fn download(
        &self,
        expected: &Entry,
        target: &Path,
        cancel: &CancellationToken,
        progress: &(dyn Fn(u64, u64) + Send + Sync),
    ) -> Result<()> {
        let id = expected.remote_id.as_ref().ok_or(Error::Stale)?;
        let response = checked(
            self.client
                .get(self.url(id)?)
                .bearer_auth(&self.token)
                .query(&[("alt", "media"), ("supportsAllDrives", "true")])
                .send()
                .await?,
        )?;
        let mut stream = response.bytes_stream();
        let mut file = File::create(target).await?;
        let mut total = 0;
        while let Some(chunk) = tokio::select! { _ = cancel.cancelled() => return Err(Error::Cancelled), chunk = stream.next() => chunk }
        {
            let chunk = chunk?;
            total += chunk.len() as u64;
            if total > expected.size {
                return Err(Error::Stale);
            }
            file.write_all(&chunk).await?;
            progress(total, expected.size);
        }
        file.sync_all().await?;
        if total != expected.size {
            return Err(Error::Stale);
        }
        Ok(())
    }
    pub async fn upload(
        &self,
        upload: Upload<'_>,
        cancel: &CancellationToken,
        progress: &(dyn Fn(u64, u64) + Send + Sync),
    ) -> Result<DriveFile> {
        let Upload {
            source,
            size,
            name,
            parent,
            existing,
            keep_forever,
        } = upload;
        let url = match existing {
            Some(f) => format!("{}/files/{}", self.upload_base, f.id),
            None => format!("{}/files", self.upload_base),
        };
        let method = if existing.is_some() {
            reqwest::Method::PATCH
        } else {
            reqwest::Method::POST
        };
        let metadata = if existing.is_some() {
            serde_json::json!({})
        } else {
            serde_json::json!({"name":name,"parents":[parent]})
        };
        let mut request = self
            .client
            .request(method, url)
            .bearer_auth(&self.token)
            .query(&[
                ("uploadType", "resumable"),
                ("supportsAllDrives", "true"),
                (
                    "keepRevisionForever",
                    if keep_forever { "true" } else { "false" },
                ),
                ("fields", FIELDS),
            ])
            .header("X-Upload-Content-Type", "application/octet-stream")
            .header("X-Upload-Content-Length", size)
            .json(&metadata);
        if let Some(etag) = existing.and_then(|f| f.etag.as_ref()) {
            request = request.header(reqwest::header::IF_MATCH, etag);
        }
        let response = checked(request.send().await?)?;
        let location = response
            .headers()
            .get(reqwest::header::LOCATION)
            .and_then(|s| s.to_str().ok())
            .ok_or_else(incomplete)?
            .to_string();
        let url = url::Url::parse(&location).map_err(|_| incomplete())?;
        let origin = url::Url::parse(&self.upload_base).map_err(|_| incomplete())?;
        if url.origin() != origin.origin() {
            return Err(incomplete());
        }
        let mut file = File::open(source).await?;
        let mut sent = 0;
        loop {
            if cancel.is_cancelled() {
                return Err(Error::Cancelled);
            }
            // 8 MiB, a multiple of Google's required 256 KiB chunk size.
            let remaining = size.saturating_sub(sent);
            let mut bytes = vec![0; remaining.min(8 * 1024 * 1024) as usize];
            file.read_exact(&mut bytes).await?;
            let length = bytes.len() as u64;
            let range = if size == 0 {
                "bytes */0".into()
            } else {
                format!("bytes {}-{}/{}", sent, sent + length - 1, size)
            };
            let response = self
                .client
                .put(&location)
                .bearer_auth(&self.token)
                .header("Content-Range", range)
                .header(reqwest::header::CONTENT_LENGTH, length)
                .body(bytes)
                .send()
                .await?;
            sent += length;
            progress(sent, size);
            if response.status().is_success() {
                return Ok(response.json().await?);
            }
            if response.status().as_u16() != 308 || sent >= size {
                return Err(Error::Provider(response.status().as_u16()));
            }
            let expected_range = format!("bytes=0-{}", sent - 1);
            if response
                .headers()
                .get("Range")
                .and_then(|v| v.to_str().ok())
                != Some(expected_range.as_str())
            {
                return Err(incomplete());
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::{
        matchers::{method, path, query_param},
        Mock, MockServer, ResponseTemplate,
    };
    #[tokio::test]
    async fn cancellation_interrupts_a_stalled_root_metadata_request() {
        let server = MockServer::start().await;
        let drive = Drive::fixture(server.uri());
        Mock::given(method("GET"))
            .and(path("/files/root"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_delay(Duration::from_secs(5))
                    .set_body_json(
                        serde_json::json!({"id":"root","name":"Root","mimeType":FOLDER}),
                    ),
            )
            .mount(&server)
            .await;
        let cancel = CancellationToken::new();
        let token = cancel.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(25)).await;
            token.cancel();
        });
        let result =
            tokio::time::timeout(Duration::from_millis(250), drive.scan("root", &cancel)).await;
        assert!(
            matches!(result, Ok(Err(Error::Cancelled))),
            "Cancellation waited for the remote metadata timeout: {result:?}"
        );
    }
    #[tokio::test]
    async fn failed_or_incomplete_pages_never_publish_partial_listing() {
        let server = MockServer::start().await;
        let drive = Drive::with_endpoints("synthetic".into(), server.uri(), server.uri()).unwrap();
        Mock::given(method("GET"))
            .and(path("/files"))
            .and(query_param("pageToken", ""))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"files":[],"nextPageToken":"two"})),
            )
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/files"))
            .and(query_param("pageToken", "two"))
            .respond_with(ResponseTemplate::new(503))
            .mount(&server)
            .await;
        assert!(drive
            .children("folder", &CancellationToken::new())
            .await
            .is_err());
        server.reset().await;
        Mock::given(method("GET"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"files":[],"incompleteSearch":true})),
            )
            .mount(&server)
            .await;
        assert!(drive
            .children("folder", &CancellationToken::new())
            .await
            .is_err());
    }
    #[tokio::test]
    async fn repeated_page_tokens_fail_instead_of_looping() {
        let server = MockServer::start().await;
        let drive = Drive::with_endpoints("synthetic".into(), server.uri(), server.uri()).unwrap();
        Mock::given(method("GET"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"files":[],"nextPageToken":"same"})),
            )
            .mount(&server)
            .await;
        assert!(drive
            .children("folder", &CancellationToken::new())
            .await
            .is_err());
    }
}
