//! Verified, disposable project previews. Callers must serialize fetch, take,
//! and purge for a project; the desktop uses its existing operation semaphore.
use crate::{drive::Drive, fs, model::now, Entry, Error, Project, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    io::Write,
    path::{Path, PathBuf},
    time::UNIX_EPOCH,
};
use tokio_util::sync::CancellationToken;

pub const MAX_BYTES: u64 = 512 * 1024 * 1024;
pub const DAY: u64 = 24 * 60 * 60;
pub const LIMIT_ERROR: &str =
    "This file exceeds the 512 MiB preview limit. Export a lighter model.";

#[derive(Serialize, Deserialize)]
struct Record {
    entry: Entry,
    created_at: u64,
}

fn directory(root: &Path) -> Result<PathBuf> {
    fs::internal_dir(root, Path::new("preview-cache"))
}

fn paths(root: &Path, expected: &Entry) -> Result<(PathBuf, PathBuf)> {
    fs::validate_relative(&expected.path)?;
    if expected.remote_id.as_ref().is_none_or(|s| s.is_empty())
        || expected.version.as_ref().is_none_or(|s| s.is_empty())
        || expected.md5.is_empty()
    {
        return Err(Error::Stale);
    }
    let identity = serde_json::to_vec(&(
        &expected.path,
        &expected.remote_id,
        &expected.version,
        &expected.md5,
        expected.size,
    ))?;
    let key = format!("{:x}", Sha256::digest(identity));
    let directory = directory(root)?;
    Ok((
        directory.join(format!("{key}.data")),
        directory.join(format!("{key}.json")),
    ))
}

fn regular(path: &Path) -> Result<bool> {
    match std::fs::symlink_metadata(path) {
        Ok(meta) if meta.is_file() && !meta.file_type().is_symlink() => Ok(true),
        Ok(_) => Err(Error::Stale),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(e.into()),
    }
}

fn discard(data: &Path, metadata: &Path) -> Result<()> {
    for path in [data, metadata] {
        if regular(path)? {
            std::fs::remove_file(path)?;
        }
    }
    Ok(())
}

fn lookup(root: &Path, expected: &Entry, cancel: &CancellationToken) -> Result<Option<PathBuf>> {
    if cancel.is_cancelled() {
        return Err(Error::Cancelled);
    }
    let (data, metadata) = paths(root, expected)?;
    if !regular(&data)? || !regular(&metadata)? {
        return Ok(None);
    }
    let record = if std::fs::metadata(&metadata)?.len() <= 65536 {
        serde_json::from_slice::<Record>(&std::fs::read(&metadata)?).ok()
    } else {
        None
    };
    let valid = record.is_some_and(|record| {
        record.entry == *expected && now().saturating_sub(record.created_at) < DAY
    });
    if valid {
        let actual = fs::inspect(
            data.parent().ok_or(Error::Stale)?,
            data.file_name()
                .and_then(|p| p.to_str())
                .ok_or(Error::Stale)?,
            cancel,
        )?;
        if actual.is_some_and(|e| e.md5 == expected.md5 && e.size == expected.size) {
            return Ok(Some(data));
        }
    }
    discard(&data, &metadata)?;
    Ok(None)
}

async fn checked_remote(
    drive: &Drive,
    expected: &Entry,
    parent: &str,
    cancel: &CancellationToken,
) -> Result<()> {
    let id = expected.remote_id.as_ref().ok_or(Error::Stale)?;
    let metadata = tokio::select! {
        _ = cancel.cancelled() => return Err(Error::Cancelled),
        result = drive.metadata(id) => result?,
    };
    if !metadata.is_some_and(|file| file.matches(expected, parent)) {
        return Err(Error::Stale);
    }
    Ok(())
}

pub async fn fetch(
    drive: &Drive,
    project: &Project,
    expected: &Entry,
    parent: &str,
    cancel: &CancellationToken,
) -> Result<PathBuf> {
    if cancel.is_cancelled() {
        return Err(Error::Cancelled);
    }
    if expected.size > MAX_BYTES {
        return Err(Error::Invalid(LIMIT_ERROR.into()));
    }
    checked_remote(drive, expected, parent, cancel).await?;
    let root = project.local_root.clone();
    let entry = expected.clone();
    let token = cancel.clone();
    let cached = tokio::task::spawn_blocking(move || lookup(&root, &entry, &token))
        .await
        .map_err(|_| Error::Stale)??;
    if let Some(cached) = cached {
        checked_remote(drive, expected, parent, cancel).await?;
        return Ok(cached);
    }
    let (data, metadata) = paths(&project.local_root, expected)?;
    // Recover orphaned/incomplete entries left by an interrupted publication.
    discard(&data, &metadata)?;
    let mut temporary = tempfile::Builder::new()
        .prefix("preview-")
        .suffix(".partial")
        .tempfile_in(data.parent().ok_or(Error::Stale)?)?;
    tokio::select! {
        _ = cancel.cancelled() => return Err(Error::Cancelled),
        result = drive.download(expected, temporary.path(), cancel, &|_, _| {}) => result?,
    }
    let target = temporary.path().to_path_buf();
    let token = cancel.clone();
    let hashed = tokio::task::spawn_blocking(move || {
        fs::inspect(
            target.parent().ok_or(Error::Stale)?,
            target
                .file_name()
                .and_then(|p| p.to_str())
                .ok_or(Error::Stale)?,
            &token,
        )
    })
    .await
    .map_err(|_| Error::Stale)??
    .ok_or(Error::Stale)?;
    if hashed.md5 != expected.md5 || hashed.size != expected.size {
        return Err(Error::Stale);
    }
    checked_remote(drive, expected, parent, cancel).await?;
    if cancel.is_cancelled() {
        return Err(Error::Cancelled);
    }
    temporary.as_file_mut().sync_all()?;
    temporary
        .persist_noclobber(&data)
        .map_err(|e| Error::Io(e.error))?;
    let mut record = tempfile::Builder::new()
        .prefix("metadata-")
        .suffix(".partial")
        .tempfile_in(metadata.parent().ok_or(Error::Stale)?)?;
    record.write_all(&serde_json::to_vec(&Record {
        entry: expected.clone(),
        created_at: now(),
    })?)?;
    record.as_file().sync_all()?;
    record
        .persist_noclobber(metadata)
        .map_err(|e| Error::Io(e.error))?;
    Ok(data)
}

/// Take ownership for a reviewed sync. The existing transfer commit then moves
/// these bytes into the real project path, or drops them on failure.
pub fn take(
    root: &Path,
    expected: &Entry,
    cancel: &CancellationToken,
) -> Result<Option<tempfile::NamedTempFile>> {
    let Some(data) = lookup(root, expected, cancel)? else {
        return Ok(None);
    };
    let (_, metadata) = paths(root, expected)?;
    let file = std::fs::File::open(&data)?;
    std::fs::remove_file(metadata)?;
    Ok(Some(tempfile::NamedTempFile::from_parts(
        file,
        tempfile::TempPath::try_from_path(data)?,
    )))
}

/// Flat cache files only: never recurse into directories or follow symlinks.
pub fn purge(root: &Path, at: u64) -> Result<usize> {
    let candidate = root.join(".permafrost/preview-cache");
    if !candidate.try_exists()? {
        return Ok(0);
    }
    let directory = directory(root)?;
    let mut removed = 0;
    for item in std::fs::read_dir(directory)? {
        let item = item?;
        let metadata = item.path().symlink_metadata()?;
        if !metadata.is_file() || metadata.file_type().is_symlink() {
            continue;
        }
        let modified = metadata
            .modified()?
            .duration_since(UNIX_EPOCH)
            .map_err(|_| Error::Stale)?
            .as_secs();
        if at.saturating_sub(modified) >= DAY {
            std::fs::remove_file(item.path())?;
            removed += 1;
        }
    }
    Ok(removed)
}

#[cfg(test)]
mod tests;
