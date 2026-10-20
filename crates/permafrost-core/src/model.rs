use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::PathBuf;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Project {
    pub id: String,
    pub name: String,
    pub local_root: PathBuf,
    pub drive_folder_id: String,
    pub read_only: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Entry {
    pub path: String,
    pub size: u64,
    pub md5: String,
    pub modified: String,
    pub remote_id: Option<String>,
    pub version: Option<String>,
    pub modified_by: Option<String>,
}

pub type Tree = BTreeMap<String, Entry>;
pub type Baseline = BTreeMap<String, String>;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ActionKind {
    UploadNew,
    DownloadNew,
    UploadRevision,
    DownloadRevision,
    DeleteLocal,
    DeleteCloud,
    UploadConflict,
    DownloadConflict,
}

impl ActionKind {
    pub fn writes_remote(self) -> bool {
        matches!(
            self,
            Self::UploadNew | Self::UploadRevision | Self::UploadConflict | Self::DeleteCloud
        )
    }
    pub fn destructive(self) -> bool {
        !matches!(self, Self::UploadNew | Self::DownloadNew)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Action {
    pub id: String,
    pub path: String,
    pub kind: ActionKind,
    pub reason: String,
    pub size: u64,
    pub local: Option<Entry>,
    pub remote: Option<Entry>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Plan {
    pub id: String,
    pub project_id: String,
    pub created_at: u64,
    pub actions: Vec<Action>,
    pub local: Tree,
    pub remote: Tree,
    pub folders: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct Settings {
    pub working_directory: PathBuf,
    pub default_drive_folder: String,
    pub keep_revisions_forever: bool,
    pub keep_local_backups: bool,
    pub close_to_tray: bool,
    pub check_updates: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            working_directory: PathBuf::new(),
            default_drive_folder: String::new(),
            keep_revisions_forever: false,
            keep_local_backups: true,
            close_to_tray: false,
            check_updates: false,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Activity {
    pub id: String,
    pub project_id: String,
    pub path: String,
    pub kind: ActionKind,
    pub state: String,
    pub message: String,
    pub created_at: u64,
}

pub fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
