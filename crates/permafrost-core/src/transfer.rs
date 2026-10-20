//! All external writes are preceded by a durable journal row. A write whose
//! outcome cannot be proved is never retried automatically.
use crate::{
    drive::{Drive, DriveFile},
    fs,
    fs::internal_dir,
    model::*,
    planner,
    store::Store,
    Error, Result,
};
use serde::Serialize;
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
};
use tokio_util::sync::CancellationToken;

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Progress {
    pub project_id: String,
    pub path: String,
    pub completed: usize,
    pub total: usize,
    pub bytes: u64,
    pub size: u64,
    pub phase: String,
}
#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Report {
    pub completed: usize,
    pub failed: usize,
    pub cancelled: bool,
}
fn database(store: &Mutex<Store>) -> Result<std::sync::MutexGuard<'_, Store>> {
    store
        .lock()
        .map_err(|_| Error::Invalid("The local database needs an application restart.".into()))
}
async fn inspect(
    project: &Project,
    path: &str,
    cancel: &CancellationToken,
) -> Result<Option<Entry>> {
    let root = project.local_root.clone();
    let path = path.to_string();
    let cancel = cancel.clone();
    tokio::task::spawn_blocking(move || fs::inspect(&root, &path, &cancel))
        .await
        .map_err(|_| Error::Invalid("A file inspection could not finish.".into()))?
}
fn local_matches(actual: &Option<Entry>, expected: &Option<Entry>) -> bool {
    actual == expected
}
fn parent_path(path: &str) -> &str {
    path.rsplit_once('/').map(|p| p.0).unwrap_or("")
}

pub struct Selection<'a> {
    pub ids: &'a [String],
    pub consent: bool,
}

pub async fn execute(
    store: &Mutex<Store>,
    drive: &Drive,
    project: &Project,
    plan: &Plan,
    selection: Selection<'_>,
    cancel: &CancellationToken,
    progress: &(dyn Fn(Progress) + Send + Sync),
) -> Result<Report> {
    let selected = planner::select(plan, project, selection.ids, selection.consent)?;
    if database(store)?.has_unresolved(&project.id)? {
        return Err(Error::RecoveryRequired);
    }
    // A complete fresh remote listing catches moved parents, disappeared files,
    // duplicates and newly created targets before any selected change is applied.
    let (remote, mut folders) = drive.scan(&project.drive_folder_id, cancel).await?;
    for action in &selected {
        if remote.get(&action.path) != action.remote.as_ref()
            || !local_matches(
                &inspect(project, &action.path, cancel).await?,
                &action.local,
            )
        {
            return Err(Error::Stale);
        }
        let mut parent = parent_path(&action.path);
        loop {
            if plan.folders.get(parent) != folders.get(parent) {
                return Err(Error::Stale);
            }
            if parent.is_empty() {
                break;
            }
            parent = parent_path(parent);
        }
    }
    // Snapshot preferences for this sync so changing Settings cannot alter the
    // recovery policy halfway through a batch.
    let settings = database(store)?.settings()?;
    let mut report = Report {
        completed: 0,
        failed: 0,
        cancelled: false,
    };
    for action in selected.iter() {
        if cancel.is_cancelled() {
            report.cancelled = true;
            break;
        }
        // Recheck local content after earlier transfers and before journaling.
        if !local_matches(
            &inspect(project, &action.path, cancel).await?,
            &action.local,
        ) {
            return Err(Error::Stale);
        }
        let id = database(store)?.begin(&project.id, action)?;
        let mutated = AtomicBool::new(false);
        let on_bytes = |bytes, size| {
            progress(Progress {
                project_id: project.id.clone(),
                path: action.path.clone(),
                completed: report.completed,
                total: selected.len(),
                bytes,
                size,
                phase: "transferring".into(),
            })
        };
        let outcome = execute_one(
            drive,
            project,
            action,
            &id,
            &mut folders,
            &settings,
            cancel,
            &on_bytes,
            &mutated,
        )
        .await;
        match outcome {
            Ok(hash) => {
                database(store)?.finish(
                    &id,
                    "completed",
                    "Transferred and verified",
                    hash.as_deref(),
                )?;
                report.completed += 1;
            }
            Err(error) => {
                let uncertain = mutated.load(Ordering::SeqCst);
                database(store)?.finish(
                    &id,
                    if uncertain { "uncertain" } else { "failed" },
                    &error.to_string(),
                    None,
                )?;
                if matches!(error, Error::Cancelled) {
                    report.cancelled = true;
                } else {
                    report.failed += 1;
                }
                // Stop at the first failure; remaining actions retain their plan
                // but need a new scan rather than an automatic stale retry.
                break;
            }
        }
        progress(Progress {
            project_id: project.id.clone(),
            path: action.path.clone(),
            completed: report.completed,
            total: selected.len(),
            bytes: action.size,
            size: action.size,
            phase: "completed".into(),
        });
    }
    Ok(report)
}

async fn observed_remote(
    drive: &Drive,
    action: &Action,
    folders: &BTreeMap<String, String>,
) -> Result<Option<DriveFile>> {
    let parent = folders.get(parent_path(&action.path));
    if let Some(expected) = &action.remote {
        let parent = parent.ok_or(Error::Stale)?;
        let file = drive
            .metadata(expected.remote_id.as_ref().ok_or(Error::Stale)?)
            .await?
            .ok_or(Error::Stale)?;
        if !file.matches(expected, parent) {
            return Err(Error::Stale);
        }
        Ok(Some(file))
    } else {
        if let Some(parent) = parent {
            let name = action.path.rsplit('/').next().ok_or(Error::Stale)?;
            if drive
                .children(parent, &CancellationToken::new())
                .await?
                .iter()
                .any(|f| fs::identity(&f.name) == fs::identity(name))
            {
                return Err(Error::Stale);
            }
        }
        Ok(None)
    }
}

async fn ensure_parent(
    drive: &Drive,
    path: &str,
    folders: &mut BTreeMap<String, String>,
    cancel: &CancellationToken,
    mutated: &AtomicBool,
) -> Result<String> {
    let mut current = String::new();
    let mut id = folders.get("").cloned().ok_or(Error::Stale)?;
    for part in path.split('/').filter(|s| !s.is_empty()) {
        current = if current.is_empty() {
            part.into()
        } else {
            format!("{current}/{part}")
        };
        if let Some(known) = folders.get(&current) {
            id = known.clone();
            continue;
        }
        if cancel.is_cancelled() {
            return Err(Error::Cancelled);
        }
        let children = drive.children(&id, cancel).await?;
        if children
            .iter()
            .any(|f| fs::identity(&f.name) == fs::identity(part))
        {
            return Err(Error::Stale);
        }
        mutated.store(true, Ordering::SeqCst);
        id = drive.create_folder(part, &id).await?;
        folders.insert(current.clone(), id.clone());
    }
    Ok(id)
}

fn backup(
    project: &Project,
    action: &Action,
    operation: &str,
    cancel: &CancellationToken,
) -> Result<PathBuf> {
    let source = fs::safe_path(&project.local_root, &action.path)?;
    if fs::inspect(&project.local_root, &action.path, cancel)? != action.local {
        return Err(Error::Stale);
    }
    let directory = internal_dir(&project.local_root, &Path::new("backups").join(operation))?;
    let target = directory.join(source.file_name().ok_or(Error::Stale)?);
    if target.exists() {
        return Err(Error::Stale);
    }
    std::fs::rename(&source, &target)?;
    // When recovery copies are enabled, retain the old local file under its
    // operation-specific backup even if a later commit fails.
    Ok(target)
}

fn remove_local(
    project: &Project,
    action: &Action,
    operation: &str,
    keep_backup: bool,
    cancel: &CancellationToken,
) -> Result<()> {
    if keep_backup {
        backup(project, action, operation, cancel)?;
    } else {
        // Disabling backups is an explicit choice to remove this content
        // permanently. Keep the same observed-version check as the backup path.
        let source = fs::safe_path(&project.local_root, &action.path)?;
        if fs::inspect(&project.local_root, &action.path, cancel)? != action.local {
            return Err(Error::Stale);
        }
        if cancel.is_cancelled() {
            return Err(Error::Cancelled);
        }
        std::fs::remove_file(source)?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn execute_one(
    drive: &Drive,
    project: &Project,
    action: &Action,
    operation: &str,
    folders: &mut BTreeMap<String, String>,
    settings: &Settings,
    cancel: &CancellationToken,
    progress: &(dyn Fn(u64, u64) + Send + Sync),
    mutated: &AtomicBool,
) -> Result<Option<String>> {
    let remote = observed_remote(drive, action, folders).await?;
    if cancel.is_cancelled() {
        return Err(Error::Cancelled);
    }
    match action.kind {
        ActionKind::UploadNew | ActionKind::UploadRevision | ActionKind::UploadConflict => {
            if project.read_only {
                return Err(Error::Invalid("This project is read-only.".into()));
            }
            let expected = action.local.as_ref().ok_or(Error::Stale)?;
            let stage_dir = internal_dir(&project.local_root, Path::new("staging"))?;
            let stage = tempfile::NamedTempFile::new_in(&stage_dir)?;
            let source = fs::safe_path(&project.local_root, &action.path)?;
            let stage_path = stage.path().to_path_buf();
            tokio::fs::copy(source, &stage_path).await?;
            let hash = inspect_staged(&stage_path, cancel).await?;
            if hash.md5 != expected.md5
                || hash.size != expected.size
                || !local_matches(
                    &inspect(project, &action.path, cancel).await?,
                    &action.local,
                )
            {
                return Err(Error::Stale);
            }
            let parent =
                ensure_parent(drive, parent_path(&action.path), folders, cancel, mutated).await?;
            let name = action.path.rsplit('/').next().ok_or(Error::Stale)?;
            let remote = observed_remote(drive, action, folders).await?;
            mutated.store(true, Ordering::SeqCst);
            let uploaded = drive
                .upload(
                    crate::drive::Upload {
                        source: stage.path(),
                        size: expected.size,
                        name,
                        parent: &parent,
                        existing: remote.as_ref(),
                        keep_forever: settings.keep_revisions_forever,
                    },
                    cancel,
                    progress,
                )
                .await?;
            let verified = drive.metadata(&uploaded.id).await?.ok_or(Error::Stale)?;
            if verified.trashed
                || verified.md5_checksum.as_ref() != Some(&expected.md5)
                || verified.name != name
                || !verified.parents.contains(&parent)
            {
                return Err(Error::Stale);
            }
            Ok(Some(expected.md5.clone()))
        }
        ActionKind::DownloadNew | ActionKind::DownloadRevision | ActionKind::DownloadConflict => {
            let expected = action.remote.as_ref().ok_or(Error::Stale)?;
            let root = project.local_root.clone();
            let entry = expected.clone();
            let token = cancel.clone();
            let cached = tokio::task::spawn_blocking(move || {
                crate::preview_cache::take(&root, &entry, &token)
            })
            .await
            .map_err(|_| Error::Stale)??;
            let stage = if let Some(cached) = cached {
                progress(expected.size, expected.size);
                cached
            } else {
                let stage_dir = internal_dir(&project.local_root, Path::new("staging"))?;
                let stage = tempfile::NamedTempFile::new_in(stage_dir)?;
                drive
                    .download(expected, stage.path(), cancel, progress)
                    .await?;
                stage
            };
            let hashed = inspect_staged(stage.path(), cancel).await?;
            if hashed.md5 != expected.md5 || hashed.size != expected.size {
                return Err(Error::Stale);
            }
            observed_remote(drive, action, folders).await?;
            if !local_matches(
                &inspect(project, &action.path, cancel).await?,
                &action.local,
            ) {
                return Err(Error::Stale);
            }
            let target = fs::safe_path(&project.local_root, &action.path)?;
            if let Some(parent) = target.parent() {
                std::fs::create_dir_all(parent)?;
            }
            fs::safe_path(&project.local_root, &action.path)?;
            if cancel.is_cancelled() {
                return Err(Error::Cancelled);
            }
            mutated.store(true, Ordering::SeqCst);
            if action.local.is_some() {
                remove_local(
                    project,
                    action,
                    operation,
                    settings.keep_local_backups,
                    cancel,
                )?;
            }
            stage
                .persist_noclobber(&target)
                .map_err(|e| Error::Io(e.error))?;
            let final_copy = inspect(project, &action.path, cancel)
                .await?
                .ok_or(Error::Stale)?;
            if final_copy.md5 != expected.md5 {
                return Err(Error::Stale);
            }
            Ok(Some(expected.md5.clone()))
        }
        ActionKind::DeleteLocal => {
            if remote.is_some() {
                return Err(Error::Stale);
            }
            mutated.store(true, Ordering::SeqCst);
            remove_local(
                project,
                action,
                operation,
                settings.keep_local_backups,
                cancel,
            )?;
            Ok(None)
        }
        ActionKind::DeleteCloud => {
            if project.read_only {
                return Err(Error::Invalid("This project is read-only.".into()));
            }
            if inspect(project, &action.path, cancel).await?.is_some() {
                return Err(Error::Stale);
            }
            mutated.store(true, Ordering::SeqCst);
            drive.trash(remote.as_ref().ok_or(Error::Stale)?).await?;
            Ok(None)
        }
    }
}
async fn inspect_staged(path: &Path, cancel: &CancellationToken) -> Result<Entry> {
    let path = path.to_path_buf();
    let cancel = cancel.clone();
    tokio::task::spawn_blocking(move || {
        let parent = path.parent().ok_or(Error::Stale)?;
        let name = path
            .file_name()
            .and_then(|s| s.to_str())
            .ok_or(Error::Stale)?;
        fs::inspect(parent, name, &cancel)?.ok_or(Error::Stale)
    })
    .await
    .map_err(|_| Error::Stale)?
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn recovery_preference_controls_local_deletion() {
        for keep_backup in [true, false] {
            let dir = tempfile::tempdir().unwrap();
            std::fs::write(dir.path().join("part.step"), b"original").unwrap();
            let cancel = CancellationToken::new();
            let project = Project {
                id: "p".into(),
                name: "p".into(),
                local_root: dir.path().into(),
                drive_folder_id: "d".into(),
                read_only: false,
            };
            let action = Action {
                id: "a".into(),
                path: "part.step".into(),
                kind: ActionKind::DeleteLocal,
                reason: "deleted".into(),
                size: 8,
                local: fs::inspect(dir.path(), "part.step", &cancel).unwrap(),
                remote: None,
            };
            remove_local(&project, &action, "operation", keep_backup, &cancel).unwrap();
            assert!(!dir.path().join("part.step").exists());
            let saved = dir.path().join(".permafrost/backups/operation/part.step");
            if keep_backup {
                assert_eq!(std::fs::read(saved).unwrap(), b"original");
            } else {
                assert!(!saved.exists());
                assert!(!dir.path().join(".permafrost").exists());
            }
        }
    }

    #[test]
    fn disabling_recovery_still_rejects_stale_local_content() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("part.step"), b"original").unwrap();
        let cancel = CancellationToken::new();
        let project = Project {
            id: "p".into(),
            name: "p".into(),
            local_root: dir.path().into(),
            drive_folder_id: "d".into(),
            read_only: false,
        };
        let action = Action {
            id: "a".into(),
            path: "part.step".into(),
            kind: ActionKind::DeleteLocal,
            reason: "deleted".into(),
            size: 8,
            local: fs::inspect(dir.path(), "part.step", &cancel).unwrap(),
            remote: None,
        };
        std::fs::write(dir.path().join("part.step"), b"changed").unwrap();
        assert!(matches!(
            remove_local(&project, &action, "operation", false, &cancel),
            Err(Error::Stale)
        ));
        assert_eq!(
            std::fs::read(dir.path().join("part.step")).unwrap(),
            b"changed"
        );
    }

    #[test]
    fn local_replacements_preserve_original_and_reject_changed_content() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("part.step"), b"original").unwrap();
        let cancel = CancellationToken::new();
        let local = fs::inspect(dir.path(), "part.step", &cancel).unwrap();
        let project = Project {
            id: "p".into(),
            name: "p".into(),
            local_root: dir.path().into(),
            drive_folder_id: "d".into(),
            read_only: false,
        };
        let action = Action {
            id: "a".into(),
            path: "part.step".into(),
            kind: ActionKind::DeleteLocal,
            reason: "deleted".into(),
            size: 8,
            local,
            remote: None,
        };
        std::fs::write(dir.path().join("part.step"), b"changed").unwrap();
        assert!(backup(&project, &action, "one", &cancel).is_err());
        std::fs::write(dir.path().join("part.step"), b"original").unwrap();
        let mut action = action;
        action.local = fs::inspect(dir.path(), "part.step", &cancel).unwrap();
        let saved = backup(&project, &action, "two", &cancel).unwrap();
        assert_eq!(std::fs::read(saved).unwrap(), b"original");
        assert!(!dir.path().join("part.step").exists());
    }
}
