//! Native file notifications work while the window is hidden. They invalidate
//! scan snapshots immediately; UI notifications are debounced to avoid rescans
//! for every write in a CAD application's multi-file save.
use super::{Backend, Project};
use notify::{Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use std::{
    collections::{BTreeSet, HashMap},
    path::Path,
    sync::mpsc,
    time::{Duration, Instant},
};
use tauri::{Emitter, Manager};

#[derive(Default)]
pub struct WatchSet {
    handles: HashMap<String, RecommendedWatcher>,
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
impl WatchSet {
    fn replace(&mut self, app: &tauri::AppHandle, projects: Vec<Project>) {
        self.handles.clear();
        let Some(sender) = self.sender.clone() else {
            return;
        };
        for project in projects {
            let root = project.local_root.clone();
            let id = project.id.clone();
            let handle = app.clone();
            let channel = sender.clone();
            let watcher = notify::recommended_watcher(move |result: notify::Result<Event>| {
                match result {
                    Ok(event) if relevant(&event, &root) => {
                        let state = handle.state::<Backend>();
                        if let Ok(mut generation) = state.generations.lock() {
                            *generation.entry(id.clone()).or_default() += 1;
                        }
                        if let Ok(mut plans) = state.plans.lock() {
                            plans.remove(&id);
                        }
                        let _ = channel.send(id.clone());
                    }
                    Err(_) => {
                        let _=handle.emit("watcher-warning","Local folder watching was interrupted. Scan manually while Permafrost reconnects the watcher.");
                    }
                    _ => {}
                }
            });
            match watcher.and_then(|mut w| {
                w.watch(&project.local_root, RecursiveMode::Recursive)?;
                Ok(w)
            }) {
                Ok(w) => {
                    self.handles.insert(project.id, w);
                }
                Err(_) => {
                    let _=app.emit("watcher-warning","A local folder is unavailable for live watching. Reconnect the folder and scan again.");
                }
            }
        }
    }
}
pub fn refresh(app: &tauri::AppHandle) {
    let state = app.state::<Backend>();
    let projects = state
        .store
        .lock()
        .ok()
        .and_then(|store| store.projects().ok());
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
        let mut interval = tokio::time::interval(Duration::from_secs(60));
        interval.tick().await;
        loop {
            interval.tick().await;
            let _ = app.emit("remote-refresh-due", ());
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
