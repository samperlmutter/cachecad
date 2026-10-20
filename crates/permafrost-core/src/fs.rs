use crate::{Entry, Error, Result, Tree};
use md5::{Digest, Md5};
use std::{
    collections::BTreeSet,
    fs::File,
    io::Read,
    path::{Component, Path, PathBuf},
};
use tokio_util::sync::CancellationToken;
use unicode_normalization::UnicodeNormalization;

/// Portable paths prevent aliases, traversal, NTFS alternate streams and Windows
/// device names from turning a Drive filename into an unintended local target.
pub fn validate_relative(path: &str) -> Result<()> {
    if path.is_empty() || path.contains('\\') || Path::new(path).is_absolute() {
        return Err(unsafe_path());
    }
    for part in path.split('/') {
        let stem = part.split('.').next().unwrap_or("").to_ascii_uppercase();
        if part.is_empty()
            || part == "."
            || part == ".."
            || part.ends_with(['.', ' '])
            || part
                .chars()
                .any(|c| c.is_control() || "<>:\"|?*".contains(c))
            || ["CON", "PRN", "AUX", "NUL"].contains(&stem.as_str())
            || (stem.len() == 4
                && (stem.starts_with("COM") || stem.starts_with("LPT"))
                && matches!(stem.as_bytes()[3], b'1'..=b'9'))
            || part.eq_ignore_ascii_case(".permafrost")
        {
            return Err(unsafe_path());
        }
    }
    if Path::new(path)
        .components()
        .any(|p| !matches!(p, Component::Normal(_)))
    {
        return Err(unsafe_path());
    }
    Ok(())
}
fn unsafe_path() -> Error {
    Error::Invalid("A project contains a filename that cannot safely sync across Windows and macOS. Rename it before syncing.".into())
}
pub fn identity(path: &str) -> String {
    path.nfc().flat_map(char::to_lowercase).collect()
}

pub fn safe_path(root: &Path, relative: &str) -> Result<PathBuf> {
    validate_relative(relative)?;
    if !root.is_absolute() || std::fs::symlink_metadata(root)?.file_type().is_symlink() {
        return Err(unsafe_path());
    }
    let canonical = root.canonicalize()?;
    let mut cursor = canonical.clone();
    for part in relative.split('/') {
        cursor.push(part);
        match std::fs::symlink_metadata(&cursor) {
            Ok(meta) if meta.file_type().is_symlink() => return Err(unsafe_path()),
            Ok(_) => {
                if !cursor.canonicalize()?.starts_with(&canonical) {
                    return Err(unsafe_path());
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
    }
    Ok(cursor)
}

/// Application-owned directories remain inside the canonical project and may
/// never traverse a symlink, including a replaced project root.
pub(crate) fn internal_dir(root: &Path, suffix: &Path) -> Result<PathBuf> {
    if std::fs::symlink_metadata(root)?.file_type().is_symlink() {
        return Err(Error::Stale);
    }
    let mut cursor = root.canonicalize()?;
    for component in Path::new(".permafrost").join(suffix).components() {
        if !matches!(component, Component::Normal(_)) {
            return Err(Error::Stale);
        }
        cursor.push(component);
        match std::fs::symlink_metadata(&cursor) {
            Ok(meta) if !meta.is_dir() || meta.file_type().is_symlink() => {
                return Err(Error::Stale)
            }
            Ok(_) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => std::fs::create_dir(&cursor)?,
            Err(e) => return Err(e.into()),
        }
    }
    Ok(cursor)
}

fn timestamp(meta: &std::fs::Metadata) -> Result<String> {
    let modified = meta
        .modified()?
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| Error::Stale)?
        .as_nanos();
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        Ok(format!(
            "{modified}:{}:{}:{}:{}",
            meta.dev(),
            meta.ino(),
            meta.ctime(),
            meta.ctime_nsec()
        ))
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        Ok(format!(
            "{modified}:{}:{}",
            meta.creation_time(),
            meta.file_attributes()
        ))
    }
    #[cfg(not(any(unix, windows)))]
    {
        Ok(modified.to_string())
    }
}

pub fn inspect(root: &Path, relative: &str, cancel: &CancellationToken) -> Result<Option<Entry>> {
    let path = safe_path(root, relative)?;
    let mut file = match File::open(&path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e.into()),
    };
    let before = file.metadata()?;
    if !before.is_file() {
        return Err(unsafe_path());
    }
    let modified = timestamp(&before)?;
    let mut digest = Md5::new();
    let mut buffer = vec![0; 1024 * 1024];
    loop {
        if cancel.is_cancelled() {
            return Err(Error::Cancelled);
        }
        let n = file.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        digest.update(&buffer[..n]);
    }
    let after = file.metadata()?;
    let named = std::fs::symlink_metadata(&path)?;
    if named.file_type().is_symlink()
        || before.len() != after.len()
        || modified != timestamp(&after)?
        || after.len() != named.len()
        || modified != timestamp(&named)?
    {
        return Err(Error::Stale);
    }
    Ok(Some(Entry {
        path: relative.into(),
        size: before.len(),
        md5: format!("{:x}", digest.finalize()),
        modified,
        remote_id: None,
        version: None,
        modified_by: None,
    }))
}

pub fn ignored(name: &str) -> bool {
    (name.eq_ignore_ascii_case(".permafrost") || name.eq_ignore_ascii_case(".cachecad"))
        || name.eq_ignore_ascii_case(".DS_Store")
        || name.eq_ignore_ascii_case("Thumbs.db")
        || name.starts_with("~$")
        || name.ends_with(".swp")
        || name.ends_with(".tmp")
}

pub fn scan(root: &Path, cancel: &CancellationToken) -> Result<Tree> {
    scan_inner(root, cancel, true, &Tree::new())
}
pub fn browse(root: &Path, cancel: &CancellationToken) -> Result<Tree> {
    scan_inner(root, cancel, false, &Tree::new())
}
/// Metadata hints only accelerate scans. Execution always rehashes selected
/// files; the native coordinator also forces a full hash pass every 15 minutes.
pub fn scan_cached(root: &Path, cancel: &CancellationToken, previous: &Tree) -> Result<Tree> {
    scan_inner(root, cancel, true, previous)
}
fn scan_inner(
    root: &Path,
    cancel: &CancellationToken,
    hash_content: bool,
    previous: &Tree,
) -> Result<Tree> {
    if !root.is_dir() {
        return Err(Error::Invalid(
            "The local project folder is unavailable. Reconnect it before scanning.".into(),
        ));
    }
    let mut result = Tree::new();
    let mut identities = BTreeSet::new();
    let walker = walkdir::WalkDir::new(root)
        .follow_links(false)
        .into_iter()
        .filter_entry(|e| e.depth() == 0 || !ignored(&e.file_name().to_string_lossy()));
    for entry in walker {
        if cancel.is_cancelled() {
            return Err(Error::Cancelled);
        }
        let entry = entry.map_err(|_| {
            Error::Invalid(
                "The local scan is incomplete. Check folder permissions and try again.".into(),
            )
        })?;
        if entry.depth() == 0 {
            continue;
        }
        if entry.file_type().is_symlink() {
            return Err(unsafe_path());
        }
        let path = entry
            .path()
            .strip_prefix(root)
            .map_err(|_| unsafe_path())?
            .components()
            .map(|c| c.as_os_str().to_str().ok_or_else(unsafe_path))
            .collect::<Result<Vec<_>>>()?
            .join("/");
        validate_relative(&path)?;
        if !identities.insert(identity(&path)) {
            return Err(Error::Invalid(
                "Two filenames collide on Windows or macOS. Rename them before syncing.".into(),
            ));
        }
        if entry.file_type().is_file() {
            let file = if hash_content {
                let meta = std::fs::metadata(safe_path(root, &path)?)?;
                if let Some(cached) = previous.get(&path).filter(|e| {
                    e.md5.len() == 32
                        && e.size == meta.len()
                        && timestamp(&meta).is_ok_and(|stamp| stamp == e.modified)
                }) {
                    cached.clone()
                } else {
                    inspect(root, &path, cancel)?.ok_or(Error::Stale)?
                }
            } else {
                let meta = std::fs::metadata(safe_path(root, &path)?)?;
                Entry {
                    path: path.clone(),
                    size: meta.len(),
                    md5: String::new(),
                    modified: timestamp(&meta)?,
                    remote_id: None,
                    version: None,
                    modified_by: None,
                }
            };
            result.insert(path, file);
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_nonportable_paths() {
        for path in [
            "../x",
            "x/../y",
            "/x",
            "C:/x",
            "x\\y",
            "x//y",
            "x.",
            "con.txt",
            "COM1",
            "a:b",
            ".permafrost/x",
        ] {
            assert!(validate_relative(path).is_err(), "{path}");
        }
        assert!(validate_relative("Assembly/Bracket v2.step").is_ok());
        assert_eq!(identity("BÄR.step"), identity("ba\u{308}r.STEP"));
    }
    #[test]
    fn cached_scans_reuse_unchanged_hashes_and_find_actual_edits() {
        let root = tempfile::tempdir().unwrap();
        let cancel = CancellationToken::new();
        std::fs::write(root.path().join("part.step"), b"first").unwrap();
        let original = scan(root.path(), &cancel).unwrap();
        assert_eq!(
            scan_cached(root.path(), &cancel, &original).unwrap(),
            original
        );
        std::fs::write(root.path().join("part.step"), b"second revision").unwrap();
        let changed = scan_cached(root.path(), &cancel, &original).unwrap();
        assert_ne!(changed["part.step"].md5, original["part.step"].md5);
        assert_eq!(changed["part.step"].size, 15);
        std::fs::remove_file(root.path().join("part.step")).unwrap();
        assert!(scan_cached(root.path(), &cancel, &changed)
            .unwrap()
            .is_empty());
    }
    #[test]
    fn offline_browsing_lists_real_models_without_hashing_their_contents() {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("model.step"), b"model content").unwrap();
        let entries = browse(root.path(), &CancellationToken::new()).unwrap();
        assert_eq!(entries["model.step"].size, 13);
        assert!(entries["model.step"].md5.is_empty());
    }
    #[test]
    fn scan_hashes_and_ignores_locks() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("part.step"), b"hello").unwrap();
        std::fs::write(temp.path().join("~$part.sldprt"), b"lock").unwrap();
        let tree = scan(temp.path(), &CancellationToken::new()).unwrap();
        assert_eq!(tree.len(), 1);
        assert_eq!(tree["part.step"].md5, "5d41402abc4b2a76b9719d911017c592");
    }
    #[cfg(unix)]
    #[test]
    fn symlink_escape_is_rejected() {
        let root = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        std::os::unix::fs::symlink(outside.path(), root.path().join("escape")).unwrap();
        assert!(safe_path(root.path(), "escape/file").is_err());
        assert!(scan(root.path(), &CancellationToken::new()).is_err());
    }
}
