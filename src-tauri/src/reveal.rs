use std::path::{Path, PathBuf};

fn local_file(root: &Path, relative: &str) -> Result<PathBuf, String> {
    let path = permafrost_core::fs::safe_path(root, relative).map_err(|e| e.to_string())?;
    let metadata = std::fs::symlink_metadata(&path).map_err(|_| {
        "This file is not on your computer. Download it before revealing it.".to_string()
    })?;
    if !metadata.file_type().is_file() {
        return Err("Choose a local file to reveal.".into());
    }
    Ok(path)
}

pub fn show(root: &Path, relative: &str) -> Result<(), String> {
    let path = local_file(root, relative)?;
    tauri_plugin_opener::reveal_item_in_dir(path)
        .map_err(|e| format!("Could not reveal this file: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reveals_only_existing_project_files_without_reading_their_contents() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path().canonicalize().unwrap();
        std::fs::create_dir(root.join("Parts 零件")).unwrap();
        let relative = "Parts 零件/Fourbar intake 2026.STEP";
        let path = root.join(relative);
        let file = std::fs::File::create(&path).unwrap();
        file.set_len(1024 * 1024 * 1024).unwrap();
        assert_eq!(local_file(&root, relative).unwrap(), path);
        assert!(local_file(&root, "missing.sldprt")
            .unwrap_err()
            .contains("Download"));
        for path in [
            "../outside.txt",
            "/outside.txt",
            ".permafrost/secret",
            "Parts 零件",
        ] {
            assert!(local_file(&root, path).is_err(), "{path}");
        }
    }
    #[cfg(unix)]
    #[test]
    fn rejects_symlinks_outside_the_project() {
        let root = tempfile::tempdir().unwrap();
        let outside = tempfile::NamedTempFile::new().unwrap();
        std::os::unix::fs::symlink(outside.path(), root.path().join("alias.step")).unwrap();
        assert!(local_file(root.path(), "alias.step").is_err());
    }
}
