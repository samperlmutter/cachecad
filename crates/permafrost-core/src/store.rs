use crate::{model::*, Error, Result};
use rusqlite::{params, Connection};
use std::path::Path;

pub struct Store {
    db: Connection,
}

impl Store {
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        Self::from_connection(Connection::open(path)?)
    }
    pub fn memory() -> Result<Self> {
        Self::from_connection(Connection::open_in_memory()?)
    }
    fn from_connection(db: Connection) -> Result<Self> {
        db.busy_timeout(std::time::Duration::from_secs(5))?;
        db.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, position INTEGER NOT NULL, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS baseline(project_id TEXT NOT NULL REFERENCES projects(id), path TEXT NOT NULL, md5 TEXT NOT NULL, PRIMARY KEY(project_id,path));
          CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL, created_at INTEGER NOT NULL, action TEXT NOT NULL);
          PRAGMA user_version=1;")?;
        // External writes may have committed before a crash. They must never be
        // replayed automatically. Review explicitly reconciles their actual state.
        db.execute("UPDATE operations SET state='uncertain',message='The app closed during this transfer. Scan and inspect both copies before resolving.' WHERE state='running'", [])?;
        Ok(Self { db })
    }
    pub fn projects(&self) -> Result<Vec<Project>> {
        let mut stmt = self
            .db
            .prepare("SELECT data FROM projects ORDER BY position,id")?;
        let data = stmt
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        data.into_iter()
            .map(|s| Ok(serde_json::from_str(&s)?))
            .collect()
    }
    pub fn project(&self, id: &str) -> Result<Project> {
        self.projects()?
            .into_iter()
            .find(|p| p.id == id)
            .ok_or_else(|| Error::Invalid("Project no longer exists.".into()))
    }
    pub fn save_project(&mut self, project: &Project) -> Result<()> {
        if project.name.trim().is_empty()
            || project.drive_folder_id.is_empty()
            || !project
                .drive_folder_id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        {
            return Err(Error::Invalid(
                "Enter a project name and a valid Google Drive folder ID.".into(),
            ));
        }
        if !project.local_root.is_absolute() || !project.local_root.is_dir() {
            return Err(Error::Invalid("Choose an existing local folder.".into()));
        }
        let canonical = project.local_root.canonicalize()?;
        for other in self.projects()? {
            if other.id == project.id {
                continue;
            }
            let root = match other.local_root.canonicalize() {
                Ok(root) => root,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    other.local_root.clone()
                }
                Err(error) => return Err(error.into()),
            };
            if canonical.starts_with(&root) || root.starts_with(&canonical) {
                return Err(Error::Invalid("Project folders must not overlap.".into()));
            }
        }
        // Editing roots would invalidate every observed version and baseline.
        if let Ok(previous) = self.project(&project.id) {
            if previous.local_root != canonical
                || previous.drive_folder_id != project.drive_folder_id
            {
                return Err(Error::Invalid(
                    "Create a new project to connect different folders.".into(),
                ));
            }
        }
        let mut stored = project.clone();
        stored.local_root = canonical;
        let data = serde_json::to_string(&stored)?;
        self.db.execute("INSERT INTO projects(id,position,data) VALUES(?1,(SELECT COALESCE(MAX(position),-1)+1 FROM projects),?2) ON CONFLICT(id) DO UPDATE SET data=excluded.data", params![project.id,data])?;
        Ok(())
    }
    pub fn remove_project(&mut self, id: &str) -> Result<()> {
        if self.has_unresolved(id)? {
            return Err(Error::RecoveryRequired);
        }
        let tx = self.db.transaction()?;
        tx.execute("DELETE FROM baseline WHERE project_id=?1", [id])?;
        tx.execute("DELETE FROM projects WHERE id=?1", [id])?;
        tx.commit()?;
        Ok(())
    }
    pub fn reorder(&mut self, ids: &[String]) -> Result<()> {
        let existing: std::collections::BTreeSet<_> =
            self.projects()?.into_iter().map(|p| p.id).collect();
        if ids
            .iter()
            .cloned()
            .collect::<std::collections::BTreeSet<_>>()
            != existing
            || ids.len() != existing.len()
        {
            return Err(Error::Invalid("Project order is out of date.".into()));
        }
        let tx = self.db.transaction()?;
        for (position, id) in ids.iter().enumerate() {
            tx.execute(
                "UPDATE projects SET position=?1 WHERE id=?2",
                params![position, id],
            )?;
        }
        tx.commit()?;
        Ok(())
    }
    pub fn settings(&self) -> Result<Settings> {
        let data: std::result::Result<String, _> =
            self.db
                .query_row("SELECT data FROM settings WHERE id=1", [], |r| r.get(0));
        match data {
            Ok(s) => Ok(serde_json::from_str(&s)?),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(Settings {
                check_updates: true,
                ..Settings::default()
            }),
            Err(e) => Err(e.into()),
        }
    }
    pub fn save_settings(&self, settings: &Settings) -> Result<()> {
        self.db.execute("INSERT INTO settings(id,data) VALUES(1,?1) ON CONFLICT(id) DO UPDATE SET data=excluded.data", [serde_json::to_string(settings)?])?;
        Ok(())
    }
    pub fn baseline(&self, project: &str) -> Result<Baseline> {
        let mut stmt = self
            .db
            .prepare("SELECT path,md5 FROM baseline WHERE project_id=?1")?;
        let entries = stmt
            .query_map([project], |r| Ok((r.get(0)?, r.get(1)?)))?
            .collect::<std::result::Result<_, _>>()?;
        Ok(entries)
    }
    pub fn record_equal(&mut self, project: &str, local: &Tree, remote: &Tree) -> Result<()> {
        let tx = self.db.transaction()?;
        for (path, l) in local {
            if remote.get(path).is_some_and(|r| r.md5 == l.md5) {
                tx.execute("INSERT INTO baseline(project_id,path,md5) VALUES(?1,?2,?3) ON CONFLICT(project_id,path) DO UPDATE SET md5=excluded.md5", params![project,path,l.md5])?;
            }
        }
        // Forget paths absent on both sides; a later new file must not inherit
        // deletion intent from an old incarnation of the same filename.
        let mut stmt = tx.prepare("SELECT path FROM baseline WHERE project_id=?1")?;
        let paths = stmt
            .query_map([project], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        drop(stmt);
        for path in paths {
            if !local.contains_key(&path) && !remote.contains_key(&path) {
                tx.execute(
                    "DELETE FROM baseline WHERE project_id=?1 AND path=?2",
                    params![project, path],
                )?;
            }
        }
        tx.commit()?;
        Ok(())
    }
    pub fn begin(&self, project: &str, action: &Action) -> Result<String> {
        let id = uuid::Uuid::new_v4().to_string();
        self.db.execute(
            "INSERT INTO operations VALUES(?1,?2,?3,?4,'running','Transfer started',?5,?6)",
            params![
                id,
                project,
                action.path,
                serde_json::to_string(&action.kind)?,
                now(),
                serde_json::to_string(action)?
            ],
        )?;
        Ok(id)
    }
    pub fn finish(
        &mut self,
        id: &str,
        state: &str,
        message: &str,
        md5: Option<&str>,
    ) -> Result<()> {
        if !["completed", "failed", "uncertain", "resolved"].contains(&state) {
            return Err(Error::Invalid("Invalid transfer state.".into()));
        }
        let tx = self.db.transaction()?;
        tx.execute(
            "UPDATE operations SET state=?1,message=?2 WHERE id=?3",
            params![state, message, id],
        )?;
        if state == "completed" {
            let (project, path): (String, String) = tx.query_row(
                "SELECT project_id,path FROM operations WHERE id=?1",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )?;
            match md5 {
                Some(hash) => {
                    tx.execute("INSERT INTO baseline VALUES(?1,?2,?3) ON CONFLICT(project_id,path) DO UPDATE SET md5=excluded.md5", params![project,path,hash])?;
                }
                None => {
                    tx.execute(
                        "DELETE FROM baseline WHERE project_id=?1 AND path=?2",
                        params![project, path],
                    )?;
                }
            }
        }
        tx.commit()?;
        Ok(())
    }
    pub fn has_unresolved(&self, project: &str) -> Result<bool> {
        Ok(self.db.query_row("SELECT EXISTS(SELECT 1 FROM operations WHERE project_id=?1 AND state IN ('running','uncertain'))", [project], |r| r.get(0))?)
    }
    pub fn activity(&self) -> Result<Vec<Activity>> {
        let mut stmt = self.db.prepare("SELECT id,project_id,path,kind,state,message,created_at FROM operations ORDER BY created_at DESC,rowid DESC LIMIT 1000")?;
        let rows = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, String>(5)?,
                    r.get::<_, u64>(6)?,
                ))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        rows.into_iter()
            .map(|(id, project_id, path, kind, state, message, created_at)| {
                Ok(Activity {
                    id,
                    project_id,
                    path,
                    kind: serde_json::from_str(&kind)?,
                    state,
                    message,
                    created_at,
                })
            })
            .collect()
    }
    pub fn resolve(&mut self, id: &str) -> Result<()> {
        let state: String =
            self.db
                .query_row("SELECT state FROM operations WHERE id=?1", [id], |r| {
                    r.get(0)
                })?;
        if state != "uncertain" {
            return Err(Error::Invalid(
                "Only interrupted transfers can be resolved.".into(),
            ));
        }
        self.finish(
            id,
            "resolved",
            "Reviewed by user. A fresh scan is required before another transfer.",
            None,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn recovery_preference_defaults_on_and_persists_an_explicit_opt_out() {
        let store = Store::memory().unwrap();
        assert!(store.settings().unwrap().keep_local_backups);
        let legacy: Settings = serde_json::from_str(r#"{"keepRevisionsForever":false}"#).unwrap();
        assert!(legacy.keep_local_backups);
        let settings = Settings {
            keep_local_backups: false,
            ..Settings::default()
        };
        store.save_settings(&settings).unwrap();
        assert!(!store.settings().unwrap().keep_local_backups);
        assert!(serde_json::to_value(settings)
            .unwrap()
            .get("keepLocalBackups")
            .is_some());
    }

    fn project(root: &Path) -> Project {
        Project {
            id: "one".into(),
            name: "Assembly".into(),
            local_root: root.canonicalize().unwrap(),
            drive_folder_id: "folder".into(),
            read_only: false,
        }
    }
    #[test]
    fn a_disconnected_project_does_not_block_other_project_settings() {
        let directory = tempfile::tempdir().unwrap();
        let missing = directory.path().join("disconnected");
        let available = directory.path().join("available");
        std::fs::create_dir_all(&missing).unwrap();
        std::fs::create_dir_all(&available).unwrap();
        let mut store = Store::memory().unwrap();
        store.save_project(&project(&missing)).unwrap();
        std::fs::remove_dir(&missing).unwrap();
        let mut other = project(&available);
        other.id = "two".into();
        store.save_project(&other).unwrap();
        other.name = "Still available".into();
        other.read_only = true;
        store.save_project(&other).unwrap();
        assert_eq!(store.project("two").unwrap(), other);
        store.remove_project("two").unwrap();
        // A disconnected child still reserves its path: reconnecting later must
        // never turn two projects into overlapping roots.
        other.id = "parent".into();
        other.local_root = directory.path().into();
        assert!(store.save_project(&other).is_err());
    }
    #[test]
    fn persistence_and_crash_recovery() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("db.sqlite");
        let mut store = Store::open(&path).unwrap();
        let project = project(dir.path());
        store.save_project(&project).unwrap();
        let a = Action {
            id: "a".into(),
            path: "part.stl".into(),
            kind: ActionKind::UploadNew,
            reason: "new".into(),
            size: 10,
            local: None,
            remote: None,
        };
        let op = store.begin(&project.id, &a).unwrap();
        drop(store);
        let mut store = Store::open(&path).unwrap();
        assert_eq!(store.projects().unwrap(), vec![project]);
        assert_eq!(store.activity().unwrap()[0].state, "uncertain");
        assert!(store.has_unresolved("one").unwrap());
        assert!(store.remove_project("one").is_err());
        store.resolve(&op).unwrap();
        assert!(!store.has_unresolved("one").unwrap());
    }
    #[test]
    fn failed_transfers_do_not_advance_baseline() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::memory().unwrap();
        store.save_project(&project(dir.path())).unwrap();
        let a = Action {
            id: "a".into(),
            path: "part.stl".into(),
            kind: ActionKind::UploadNew,
            reason: "new".into(),
            size: 10,
            local: None,
            remote: None,
        };
        let op = store.begin("one", &a).unwrap();
        store
            .finish(&op, "failed", "offline", Some("wrong"))
            .unwrap();
        assert!(store.baseline("one").unwrap().is_empty());
        let op = store.begin("one", &a).unwrap();
        store
            .finish(&op, "completed", "verified", Some("correct"))
            .unwrap();
        assert_eq!(store.baseline("one").unwrap()["part.stl"], "correct");
    }
    #[test]
    fn seeded_shutdowns_preserve_baselines_and_never_replay_uncertain_writes() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("journal.sqlite");
        let project = project(root.path());
        Store::open(&path).unwrap().save_project(&project).unwrap();
        let mut expected = None;
        let mut seed = 0x5045_524du32;
        for cycle in 0..128 {
            seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
            let action = Action {
                id: format!("action-{cycle}"),
                path: "assembly.step".into(),
                kind: ActionKind::UploadRevision,
                reason: "QA".into(),
                size: 1,
                local: None,
                remote: None,
            };
            let mut store = Store::open(&path).unwrap();
            let operation = store.begin(&project.id, &action).unwrap();
            let state = seed % 3;
            let hash = format!("verified-{cycle}");
            match state {
                0 => {} // Simulate process loss before recording a result.
                1 => store
                    .finish(&operation, "failed", "Injected interrupted transfer", None)
                    .unwrap(),
                _ => {
                    store
                        .finish(&operation, "completed", "Verified", Some(&hash))
                        .unwrap();
                    expected = Some(hash);
                }
            }
            drop(store);
            let mut store = Store::open(&path).unwrap();
            assert_eq!(
                store.baseline(&project.id).unwrap().get("assembly.step"),
                expected.as_ref()
            );
            let activity = store.activity().unwrap();
            let restored = activity.iter().find(|item| item.id == operation).unwrap();
            if state == 0 {
                assert_eq!(restored.state, "uncertain");
                assert!(store.has_unresolved(&project.id).unwrap());
                store.resolve(&operation).unwrap();
            } else {
                assert_eq!(
                    restored.state,
                    if state == 1 { "failed" } else { "completed" }
                );
            }
            assert!(!store.has_unresolved(&project.id).unwrap());
        }
    }
}
