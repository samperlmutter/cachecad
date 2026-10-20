use std::{io::Read, path::Path};

const PREVIEW_BYTE_LIMIT: u64 = 512 * 1024 * 1024;
const LIMIT_ERROR: &str = "This file exceeds the 512 MiB preview limit. Export a lighter model.";

pub fn read(root: &Path, path: &str) -> Result<Vec<u8>, String> {
    let ext = Path::new(path)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if ![
        "stl", "step", "stp", "iges", "igs", "brep", "brp", "obj", "glb",
    ]
    .contains(&ext.as_str())
    {
        return Err("Preview a STEP, IGES, BREP, STL, OBJ, or GLB export of this file.".into());
    }
    let target = permafrost_core::fs::safe_path(root, path).map_err(|e| e.to_string())?;
    let file = std::fs::File::open(target).map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            "This file is not on your computer yet. Download it before previewing.".to_string()
        } else {
            format!("Could not open this local model: {error}")
        }
    })?;
    let metadata = file.metadata().map_err(|e| e.to_string())?;
    if !metadata.is_file() {
        return Err("Choose a model file to preview.".into());
    }
    // Reject known oversized files before allocating or reading their contents.
    if metadata.len() > PREVIEW_BYTE_LIMIT {
        return Err(LIMIT_ERROR.into());
    }
    let mut bytes = Vec::new();
    file.take(PREVIEW_BYTE_LIMIT + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() as u64 > PREVIEW_BYTE_LIMIT {
        return Err(LIMIT_ERROR.into());
    }
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn reads_step_files_above_the_old_64_mib_limit() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("assembly.STEP");
        let mut file = std::fs::File::create(path).unwrap();
        file.write_all(b"ISO-10303-21;").unwrap();
        file.set_len(70 * 1024 * 1024).unwrap();
        let bytes = read(directory.path(), "assembly.STEP").unwrap();
        assert_eq!(bytes.len(), 70 * 1024 * 1024);
        assert!(bytes.starts_with(b"ISO-10303-21;"));
    }

    #[test]
    fn rejects_files_above_512_mib_with_an_explanation() {
        let directory = tempfile::tempdir().unwrap();
        let file = std::fs::File::create(directory.path().join("huge.step")).unwrap();
        file.set_len(512 * 1024 * 1024 + 1).unwrap();
        assert_eq!(
            read(directory.path(), "huge.step").unwrap_err(),
            "This file exceeds the 512 MiB preview limit. Export a lighter model."
        );
    }

    #[test]
    fn missing_files_explain_that_a_download_is_required() {
        let directory = tempfile::tempdir().unwrap();
        assert_eq!(
            read(directory.path(), "remote.step").unwrap_err(),
            "This file is not on your computer yet. Download it before previewing."
        );
    }
}
