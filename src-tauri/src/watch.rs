//! Native watches invalidate snapshots immediately and debounce UI refreshes.
//! A separate native timer survives hidden/tray windows and repairs watches
//! after disconnected/replaced folders return.
use super::{Backend, Project};
use notify::{Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use std::{
    collections::{BTreeSet, HashMap},
    path::Path,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    time::{Duration, Instant},
};
use tauri::{Emitter, Manager};

#[derive(Default)]
pub struct WatchSet {
    handles: HashMap<String, (RecommendedWatcher, String, Arc<AtomicBool>)>,
    sender: Option<mpsc::Sender<String>>,
}
fn relevant(event: &Event, root: &Path) -> bool {
    !matches!(event.kind, EventKind::Access(_))
        && event
            .paths
            .iter()
            .any(|path| match path.strip_prefix(root) {
                Ok(relative) => !relative.components().any(|c| {
                    c.as_os_str()
                        .to_str()
                        .is_some_and(permafrost_core::fs::ignored)
                }),
                Err(_) => true,
            })
}
fn root_identity(root: &Path) -> Option<String> {
    let meta = std::fs::symlink_metadata(root).ok()?;
    if !meta.is_dir() || meta.file_type().is_symlink() {
        return None;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        Some(format!("{}:{}", meta.dev(), meta.ino()))
    }
    #[cfg(not(unix))]
    {
        Some(format!("{:?}", meta.created().ok()?))
    }
}
fn invalidate(app: &tauri::AppHandle, id: &str, discard_cache: bool) {
    let state = app.state::<Backend>();
    if let Ok(mut generations) = state.generations.lock() {
        *generations.entry(id.into()).or_default() += 1;
    }
    if let Ok(mut plans) = state.plans.lock() {
        plans.remove(id);
    }
    if discard_cache {
        if let Ok(mut cache) = state.scan_cache.lock() {
            cache.remove(id);
        }
    }
}
impl WatchSet {
    fn replace(&mut self, app: &tauri::AppHandle, projects: Vec<Project>) {
        self.handles.retain(|id, (_, identity, broken)| {
            !broken.load(Ordering::SeqCst)
                && projects
                    .iter()
                    .any(|p| &p.id == id && root_identity(&p.local_root).as_ref() == Some(identity))
        });
        let Some(sender) = self.sender.clone() else {
            return;
        };
        for project in projects {
            if self.handles.contains_key(&project.id) {
                continue;
            }
            let Some(identity) = root_identity(&project.local_root) else {
                let _ = app.emit(
                    "watcher-warning",
                    "A local folder is unavailable. Live watching will reconnect when it returns.",
                );
                continue;
            };
            let root = project.local_root.clone();
            let id = project.id.clone();
            let handle = app.clone();
            let channel = sender.clone();
            let broken = Arc::new(AtomicBool::new(false));
            let flag = broken.clone();
            let watcher = notify::recommended_watcher(move |result: notify::Result<Event>| {
                match result {
                    Ok(event) if relevant(&event, &root) => {
                        invalidate(&handle, &id, false);
                        let _ = channel.send(id.clone());
                    }
                    Err(_) => {
                        flag.store(true, Ordering::SeqCst);
                        invalidate(&handle, &id, true);
                        let _ = channel.send(id.clone());
                        let _=handle.emit("watcher-warning","Local watching was interrupted. Permafrost is reconnecting and will verify files again.");
                    }
                    _ => {}
                }
            });
            match watcher.and_then(|mut w| {
                w.watch(&project.local_root, RecursiveMode::Recursive)?;
                Ok(w)
            }) {
                Ok(w) => {
                    invalidate(app, &project.id, true);
                    let _ = sender.send(project.id.clone());
                    self.handles.insert(project.id, (w, identity, broken));
                }
                Err(_) => {
                    let _ = app.emit(
                        "watcher-warning",
                        "Live folder watching is unavailable. Permafrost will retry automatically.",
                    );
                }
            }
        }
    }
}
pub fn refresh(app: &tauri::AppHandle) {
    let state = app.state::<Backend>();
    let projects = state.store.lock().ok().and_then(|s| s.projects().ok());
    if let Some(projects) = projects {
        if let Ok(mut watchers) = state.watchers.lock() {
            watchers.replace(app, projects);
        }
    }
}
pub fn start(app: tauri::AppHandle) {
    let (sender, receiver) = mpsc::channel();
    if let Ok(mut watchers) = app.state::<Backend>().watchers.lock() {
        watchers.sender = Some(sender);
    }
    refresh(&app);
    let handle = app.clone();
    std::thread::spawn(move || {
        while let Ok(first) = receiver.recv() {
            let mut changed = BTreeSet::from([first]);
            let started = Instant::now();
            while started.elapsed() < Duration::from_secs(2) {
                match receiver.recv_timeout(Duration::from_millis(350)) {
                    Ok(id) => {
                        changed.insert(id);
                    }
                    Err(_) => break,
                }
            }
            let _ = handle.emit("local-change", changed.into_iter().collect::<Vec<_>>());
        }
    });
    tauri::async_runtime::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(10));
        interval.tick().await;
        let mut ticks = 0;
        loop {
            interval.tick().await;
            refresh(&app);
            ticks += 1;
            if ticks % 6 == 0 {
                let _ = app.emit("remote-refresh-due", ());
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn native_notifications_observe_an_actual_file_save() {
        let root = tempfile::tempdir().unwrap();
        let (sender, receiver) = mpsc::channel();
        let watched = root.path().to_path_buf();
        let mut watcher = notify::recommended_watcher(move |event: notify::Result<Event>| {
            if let Ok(event) = event {
                if relevant(&event, &watched) {
                    let _ = sender.send(event);
                }
            }
        })
        .unwrap();
        watcher
            .watch(root.path(), RecursiveMode::Recursive)
            .unwrap();
        let path = root.path().join("new-model.step");
        std::fs::write(&path, b"saved CAD content").unwrap();
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut observed = false;
        while Instant::now() < deadline {
            if let Ok(event) = receiver.recv_timeout(Duration::from_millis(500)) {
                if event
                    .paths
                    .iter()
                    .any(|p| p.file_name() == path.file_name() || p == root.path())
                {
                    observed = true;
                    break;
                }
            }
        }
        assert!(
            observed,
            "The native filesystem watcher did not observe an actual save"
        );
    }
    #[test]
    fn cad_changes_count_but_access_and_internal_staging_do_not() {
        let root = Path::new("/project");
        assert!(relevant(
            &Event::new(EventKind::Any).add_path(root.join("part.step")),
            root
        ));
        assert!(!relevant(
            &Event::new(EventKind::Any).add_path(root.join(".permafrost/staging/bytes")),
            root
        ));
        assert!(!relevant(
            &Event::new(EventKind::Any).add_path(root.join("~$part.sldprt")),
            root
        ));
        assert!(!relevant(
            &Event::new(EventKind::Access(notify::event::AccessKind::Any))
                .add_path(root.join("part.step")),
            root
        ));
        assert!(relevant(
            &Event::new(EventKind::Any).add_path(root.to_path_buf()),
            root
        ));
    }
}
