use permafrost_core::preview_cache;
use std::{io::Read, path::Path, time::Duration};
use tauri::{Emitter, Manager};

const PREVIEW_BYTE_LIMIT: u64 = preview_cache::MAX_BYTES;
const LIMIT_ERROR: &str = preview_cache::LIMIT_ERROR;

pub fn validate(path: &str) -> Result<(), String> {
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
    Ok(())
}

pub fn read(root: &Path, path: &str) -> Result<Vec<u8>, String> {
    validate(path)?;
    let target = permafrost_core::fs::safe_path(root, path).map_err(|e| e.to_string())?;
    read_file(&target)
}

pub fn read_file(target: &Path) -> Result<Vec<u8>, String> {
    let file = permafrost_core::fs::open_regular(target).map_err(|error| {
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

/// Startup and daily cleanup share the operation gate with preview downloads
/// and reviewed syncs, so no active cache file can be purged or promoted twice.
pub fn start_cleanup(app: tauri::AppHandle) {
    tauri::async_runtime::spawn(async move {
        let mut timer = tokio::time::interval(Duration::from_secs(preview_cache::DAY));
        timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            timer.tick().await;
            let state = app.state::<super::Backend>();
            let Ok(_permit) = state.gate.clone().acquire_owned().await else {
                break;
            };
            let projects =
                super::lock(&state.store).and_then(|store| store.projects().map_err(super::err));
            let errors = match projects {
                Ok(projects) => tauri::async_runtime::spawn_blocking(move || {
                    projects
                        .into_iter()
                        .filter_map(|project| {
                            preview_cache::purge(&project.local_root, permafrost_core::now())
                                .err()
                                .map(|error| {
                                    format!(
                                        "Could not clean preview cache for {}: {error}",
                                        project.name
                                    )
                                })
                        })
                        .collect::<Vec<_>>()
                })
                .await
                .unwrap_or_else(|_| vec!["Preview cache cleanup could not finish.".into()]),
                Err(error) => vec![error],
            };
            let warning = (!errors.is_empty()).then(|| errors.join("; "));
            if let Ok(mut retained) = super::lock(&state.preview_cache_error) {
                *retained = warning.clone();
            }
            if let Some(warning) = warning {
                let _ = app.emit("watcher-warning", warning);
            }
        }
    });
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
