//! Content-addressed converted previews. Source snapshots and process lifetime
//! are independent of the verified Drive download cache used by reviewed sync.
use permafrost_core::fs::open_regular;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    fs,
    io::{BufRead, BufReader, Read, Write},
    path::Path,
    process::{Command, Stdio},
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

const PROTOCOL: &str = "permafrost-occt-7.9.3-merged-v1";
const MAX_OUTPUT: u64 = 192 * 1024 * 1024;
const CACHE_BUDGET: u64 = 1024 * 1024 * 1024;
const DEADLINE: Duration = Duration::from_secs(120);
const CANCELLED: &str = "The operation was cancelled.";
type Result<T> = std::result::Result<T, String>;
type Progress = Arc<dyn Fn(&str) + Send + Sync>;
type Jobs = Arc<Mutex<HashMap<Uuid, (Arc<CancellationToken>, Instant, bool)>>>;

pub struct Engine {
    jobs: Jobs,
    gate: Arc<Semaphore>,
}
pub struct Job {
    id: Uuid,
    jobs: Jobs,
    pub cancel: Arc<CancellationToken>,
}
impl Default for Engine {
    fn default() -> Self {
        Self {
            jobs: Arc::default(),
            gate: Arc::new(Semaphore::new(1)),
        }
    }
}
impl Engine {
    pub fn job(&self, id: &str) -> Result<Job> {
        let id = Uuid::parse_str(id).map_err(|_| "Invalid preview request.".to_string())?;
        let mut jobs = self
            .jobs
            .lock()
            .map_err(|_| "Restart the application.".to_string())?;
        jobs.retain(|_, (token, created, active)| {
            *active || !token.is_cancelled() || created.elapsed() < Duration::from_secs(60)
        });
        if jobs.len() >= 128 {
            return Err("Too many preview requests. Close the preview and try again.".into());
        }
        let entry = jobs
            .entry(id)
            .or_insert_with(|| (Arc::new(CancellationToken::new()), Instant::now(), false));
        if entry.2 {
            return Err("This preview request is already active.".into());
        }
        entry.2 = true;
        Ok(Job {
            id,
            jobs: self.jobs.clone(),
            cancel: entry.0.clone(),
        })
    }
    pub fn cancel(&self, id: &str) -> Result<()> {
        let id = Uuid::parse_str(id).map_err(|_| "Invalid preview request.".to_string())?;
        let mut jobs = self
            .jobs
            .lock()
            .map_err(|_| "Restart the application.".to_string())?;
        if let Some((token, _, _)) = jobs.get(&id) {
            token.cancel();
        } else if jobs.len() < 128 {
            jobs.insert(
                id,
                (Arc::new(CancellationToken::new()), Instant::now(), false),
            );
            jobs[&id].0.cancel();
        }
        Ok(())
    }
    pub async fn acquire(&self, cancel: &CancellationToken) -> Result<OwnedSemaphorePermit> {
        tokio::select! { biased;
            _ = cancel.cancelled() => Err(CANCELLED.into()),
            permit = self.gate.clone().acquire_owned() => permit.map_err(|_| CANCELLED.into())
        }
    }
}
impl Drop for Engine {
    fn drop(&mut self) {
        if let Ok(jobs) = self.jobs.lock() {
            for (token, _, _) in jobs.values() {
                token.cancel();
            }
        }
    }
}
impl Drop for Job {
    fn drop(&mut self) {
        self.cancel.cancel();
        if let Ok(mut jobs) = self.jobs.lock() {
            if jobs
                .get(&self.id)
                .is_some_and(|(token, _, _)| Arc::ptr_eq(token, &self.cancel))
            {
                jobs.remove(&self.id);
            }
        }
    }
}
pub fn supported(path: &str) -> bool {
    Path::new(path)
        .extension()
        .and_then(|s| s.to_str())
        .is_some_and(|s| {
            matches!(
                s.to_ascii_lowercase().as_str(),
                "step" | "stp" | "iges" | "igs" | "brep" | "brp"
            )
        })
}
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn io(error: impl std::fmt::Display) -> String {
    format!("Could not prepare the local CAD preview: {error}")
}
fn check(cancel: &CancellationToken) -> Result<()> {
    if cancel.is_cancelled() {
        Err(CANCELLED.into())
    } else {
        Ok(())
    }
}
fn bytes(path: &Path, max: u64) -> Result<Vec<u8>> {
    let file = open_regular(path).map_err(io)?;
    if file.metadata().map_err(io)?.len() > max {
        return Err("The converted preview is too large.".into());
    }
    let mut data = Vec::new();
    file.take(max + 1).read_to_end(&mut data).map_err(io)?;
    if data.len() as u64 > max {
        return Err("The converted preview is too large.".into());
    }
    Ok(data)
}
fn glb(data: &[u8]) -> bool {
    data.len() >= 20
        && &data[..4] == b"glTF"
        && u32::from_le_bytes(data[4..8].try_into().unwrap()) == 2
        && u32::from_le_bytes(data[8..12].try_into().unwrap()) as usize == data.len()
}
fn hash(data: &[u8]) -> String {
    format!("{:x}", Sha256::digest(data))
}
#[derive(Serialize, Deserialize)]
struct Record {
    sha256: String,
    size: u64,
    used_at: u64,
}
fn publish(path: &Path, data: &[u8]) -> Result<()> {
    let mut temp = tempfile::NamedTempFile::new_in(path.parent().ok_or("Invalid preview cache.")?)
        .map_err(io)?;
    temp.write_all(data).map_err(io)?;
    temp.as_file().sync_all().map_err(io)?;
    // Only generated, flat cache entries are replaced. No project source is moved.
    if fs::symlink_metadata(path).is_ok() {
        fs::remove_file(path).map_err(io)?;
    }
    temp.persist_noclobber(path).map_err(|e| io(e.error))?;
    Ok(())
}
fn prune(root: &Path, keep: &str) -> Result<()> {
    let mut entries = Vec::new();
    for entry in fs::read_dir(root).map_err(io)? {
        let entry = entry.map_err(io)?;
        let path = entry.path();
        let Some(stem) = path.file_stem().and_then(|s| s.to_str()) else {
            continue;
        };
        if path.extension().is_none_or(|s| s != "glb")
            || stem.len() != 64
            || !stem.bytes().all(|c| c.is_ascii_hexdigit())
        {
            continue;
        }
        let metadata = fs::symlink_metadata(&path).map_err(io)?;
        if !metadata.is_file() || metadata.file_type().is_symlink() {
            continue;
        }
        let used = bytes(&path.with_extension("json"), 4096)
            .ok()
            .and_then(|b| serde_json::from_slice::<Record>(&b).ok())
            .map(|r| r.used_at)
            .unwrap_or(0);
        let stem = stem.to_string();
        entries.push((used, metadata.len(), path, stem));
    }
    entries.sort_by_key(|e| e.0);
    let mut total: u64 = entries.iter().map(|e| e.1).sum();
    for (used, size, path, key) in entries {
        if key == keep || (total <= CACHE_BUDGET && now().saturating_sub(used) < 30 * 86400) {
            continue;
        }
        fs::remove_file(&path).map_err(io)?;
        if fs::symlink_metadata(path.with_extension("json")).is_ok() {
            fs::remove_file(path.with_extension("json")).map_err(io)?;
        }
        total = total.saturating_sub(size);
    }
    Ok(())
}
pub struct Converted {
    pub data: Vec<u8>,
    pub cached: bool,
}

pub fn convert(
    root: &Path,
    mut input: fs::File,
    extension: &str,
    helper: &Path,
    cancel: &CancellationToken,
    progress: Progress,
) -> Result<Converted> {
    check(cancel)?;
    fs::create_dir_all(root).map_err(io)?;
    let metadata = fs::symlink_metadata(root).map_err(io)?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err("The CAD cache folder is unavailable.".into());
    }
    let work = tempfile::Builder::new()
        .prefix("permafrost-cad-")
        .tempdir()
        .map_err(io)?;
    let snapshot = work.path().join("source.data");
    let before = input.metadata().map_err(io)?;
    if before.len() > permafrost_core::preview_cache::MAX_BYTES {
        return Err(permafrost_core::preview_cache::LIMIT_ERROR.into());
    }
    let mut output = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&snapshot)
        .map_err(io)?;
    let mut digest = Sha256::new();
    digest.update(PROTOCOL);
    digest.update([0]);
    digest.update(extension);
    digest.update([0]);
    let mut block = [0; 65536];
    let mut copied = 0u64;
    loop {
        check(cancel)?;
        let n = input.read(&mut block).map_err(io)?;
        if n == 0 {
            break;
        }
        copied += n as u64;
        if copied > permafrost_core::preview_cache::MAX_BYTES {
            return Err(permafrost_core::preview_cache::LIMIT_ERROR.into());
        }
        digest.update(&block[..n]);
        output.write_all(&block[..n]).map_err(io)?;
    }
    let after = input.metadata().map_err(io)?;
    if copied != before.len()
        || before.len() != after.len()
        || before.modified().map_err(io)? != after.modified().map_err(io)?
    {
        return Err("This model changed while opening. Reopen the preview.".into());
    }
    drop(output);
    let key = format!("{:x}", digest.finalize());
    let model = root.join(format!("{key}.glb"));
    let record = root.join(format!("{key}.json"));
    if let (Ok(data), Ok(metadata)) = (bytes(&model, MAX_OUTPUT), bytes(&record, 4096)) {
        if let Ok(mut saved) = serde_json::from_slice::<Record>(&metadata) {
            if saved.size == data.len() as u64 && saved.sha256 == hash(&data) && glb(&data) {
                check(cancel)?;
                saved.used_at = now();
                publish(&record, &serde_json::to_vec(&saved).map_err(io)?)?;
                progress("cached");
                prune(root, &key)?;
                return Ok(Converted { data, cached: true });
            }
        }
    }
    check(cancel)?;
    progress("converting");
    let converted = work.path().join("preview.glb");
    let mut command = Command::new(helper);
    command
        .args(["--source"])
        .arg(&snapshot)
        .args(["--extension", extension, "--output"])
        .arg(&converted)
        .arg("--parent")
        .arg(std::process::id().to_string());
    command
        .env_clear()
        .current_dir(work.path())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    for variable in ["SystemRoot", "WINDIR", "TEMP", "TMP", "TMPDIR"] {
        if let Some(value) = std::env::var_os(variable) {
            command.env(variable, value);
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let child = command.spawn().map_err(|_| {
        "The bundled CAD converter could not start. Reinstall Permafrost.".to_string()
    })?;
    struct Child(std::process::Child);
    impl Drop for Child {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let mut child = Child(child);
    let stdout = child
        .0
        .stdout
        .take()
        .ok_or("The CAD converter did not open.")?;
    let read_progress = progress.clone();
    let reader = std::thread::spawn(move || {
        // Bounded diagnostics: do not expose CAD names or native parser output.
        for line in BufReader::new(stdout).take(65536).split(b'\n').flatten() {
            let Some(json) = line.strip_prefix(b"PF_CAD ") else {
                continue;
            };
            let Ok(value) = serde_json::from_slice::<serde_json::Value>(json) else {
                continue;
            };
            if let Some(phase @ ("reading" | "meshing" | "writing")) =
                value.get("phase").and_then(|v| v.as_str())
            {
                read_progress(phase);
            }
        }
    });
    let started = Instant::now();
    let result: Result<()> = loop {
        if cancel.is_cancelled() {
            break Err(CANCELLED.into());
        }
        if started.elapsed() > DEADLINE {
            break Err("This model took too long to convert. Try a simpler export.".into());
        }
        if fs::metadata(&converted).is_ok_and(|m| m.len() > MAX_OUTPUT) {
            break Err("The converted preview is too large. Export a lighter model.".into());
        }
        match child.0.try_wait().map_err(io)? {
            Some(status) if status.success() => break Ok(()),
            Some(_) => break Err(
                "OpenCascade could not convert this model. Check the file or try a fresh export."
                    .into(),
            ),
            None => std::thread::sleep(Duration::from_millis(50)),
        }
    };
    drop(child);
    let _ = reader.join();
    result?;
    check(cancel)?;
    let data = bytes(&converted, MAX_OUTPUT)?;
    if !glb(&data) {
        return Err("The CAD converter returned an invalid preview.".into());
    }
    let metadata = Record {
        sha256: hash(&data),
        size: data.len() as u64,
        used_at: now(),
    };
    publish(&model, &data)?;
    publish(&record, &serde_json::to_vec(&metadata).map_err(io)?)?;
    prune(root, &key)?;
    check(cancel)?;
    Ok(Converted {
        data,
        cached: false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    fn convert(
        root: &Path,
        source: &Path,
        extension: &str,
        helper: &Path,
        cancel: &CancellationToken,
        progress: Progress,
    ) -> Result<Converted> {
        super::convert(
            root,
            open_regular(source).map_err(io)?,
            extension,
            helper,
            cancel,
            progress,
        )
    }
    #[test]
    fn cancellation_before_registration_is_retained() {
        let engine = Engine::default();
        let id = Uuid::new_v4().to_string();
        engine.cancel(&id).unwrap();
        let job = engine.job(&id).unwrap();
        assert!(job.cancel.is_cancelled());
        drop(job);
        assert!(engine.jobs.lock().unwrap().is_empty());
    }
    #[test]
    fn drop_cancels_and_releases_only_its_request() {
        let engine = Engine::default();
        let a = engine.job(&Uuid::new_v4().to_string()).unwrap();
        let b = engine.job(&Uuid::new_v4().to_string()).unwrap();
        let token = a.cancel.clone();
        drop(a);
        assert!(token.is_cancelled());
        assert!(!b.cancel.is_cancelled());
        assert_eq!(engine.jobs.lock().unwrap().len(), 1);
    }
    #[test]
    #[ignore = "requires npm run desktop:setup-cad; exercised explicitly by installer CI"]
    fn packaged_converter_reuses_verified_cache_without_touching_source() {
        let helper = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("resources/cad-helper/permafrost-cad")
            .join(if cfg!(windows) {
                "permafrost-cad.exe"
            } else {
                "permafrost-cad"
            });
        let source =
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../desktop/tests/fixtures/cube.step");
        let original = fs::read(&source).unwrap();
        let root = tempfile::tempdir().unwrap();
        let cancel = CancellationToken::new();
        let progress: Progress = Arc::new(|_| {});
        let first = convert(
            root.path(),
            &source,
            "step",
            &helper,
            &cancel,
            progress.clone(),
        )
        .unwrap();
        assert!(!first.cached);
        assert!(glb(&first.data));
        let missing = root.path().join("missing-converter");
        let second = convert(
            root.path(),
            &source,
            "step",
            &missing,
            &cancel,
            progress.clone(),
        )
        .unwrap();
        assert!(second.cached);
        assert_eq!(first.data, second.data);
        assert_eq!(fs::read(&source).unwrap(), original);
        // Changed bytes must miss, even when the path and file size are equal.
        let changed = root.path().join("changed.step");
        let mut edit = original.clone();
        edit[0] ^= 1;
        fs::write(&changed, edit).unwrap();
        assert!(convert(
            root.path(),
            &changed,
            "step",
            &missing,
            &cancel,
            progress.clone()
        )
        .is_err());
        let aborted = CancellationToken::new();
        aborted.cancel();
        assert!(convert(
            root.path(),
            &source,
            "step",
            &helper,
            &aborted,
            progress.clone()
        )
        .is_err());
        let model = fs::read_dir(root.path())
            .unwrap()
            .map(|e| e.unwrap().path())
            .find(|p| p.extension().is_some_and(|x| x == "glb"))
            .unwrap();
        fs::write(model, b"corrupt").unwrap();
        assert!(convert(
            root.path(),
            &source,
            "step",
            &missing,
            &cancel,
            progress.clone()
        )
        .is_err());
        let recovered = convert(root.path(), &source, "step", &helper, &cancel, progress).unwrap();
        assert!(!recovered.cached);
        assert!(glb(&recovered.data));
    }
}
