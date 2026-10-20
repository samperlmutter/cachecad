//! Pure three-way comparison. A missing file means deletion only when the
//! surviving side still matches the last successfully synchronized content.
use crate::{model::*, Error, Result};
use std::collections::{BTreeMap, BTreeSet};
use uuid::Uuid;

pub fn plan(
    project: &Project,
    local: Tree,
    remote: Tree,
    baseline: &Baseline,
    folders: BTreeMap<String, String>,
) -> Plan {
    let paths: BTreeSet<_> = local.keys().chain(remote.keys()).cloned().collect();
    let mut actions = Vec::new();
    for path in paths {
        let l = local.get(&path);
        let r = remote.get(&path);
        let old = baseline.get(&path);
        let mut add = |kind: ActionKind, reason: &str| {
            if project.read_only && kind.writes_remote() {
                return;
            }
            let size = if kind.writes_remote() {
                l.or(r)
            } else {
                r.or(l)
            }
            .map(|e| e.size)
            .unwrap_or(0);
            actions.push(Action {
                id: Uuid::new_v4().to_string(),
                path: path.clone(),
                kind,
                reason: reason.into(),
                size,
                local: l.cloned(),
                remote: r.cloned(),
            });
        };
        match (l, r) {
            (Some(l), Some(r)) if l.md5 == r.md5 => {}
            (None, Some(r)) => {
                if old == Some(&r.md5) && !project.read_only {
                    add(
                        ActionKind::DeleteCloud,
                        "Deleted locally since the last sync",
                    );
                } else {
                    add(ActionKind::DownloadNew, "Available in Google Drive");
                }
            }
            (Some(l), None) => {
                if old == Some(&l.md5) {
                    add(
                        ActionKind::DeleteLocal,
                        "Deleted from Drive since the last sync",
                    );
                } else {
                    add(ActionKind::UploadNew, "Available on this computer");
                }
            }
            (Some(l), Some(r)) => {
                if old == Some(&l.md5) {
                    add(
                        ActionKind::DownloadRevision,
                        "Newer content in Google Drive",
                    );
                } else if old == Some(&r.md5) && !project.read_only {
                    add(ActionKind::UploadRevision, "Changed on this computer");
                } else {
                    add(
                        ActionKind::UploadConflict,
                        "Both copies differ — choose which one to keep",
                    );
                    add(
                        ActionKind::DownloadConflict,
                        "Both copies differ — choose which one to keep",
                    );
                }
            }
            (None, None) => unreachable!(),
        }
    }
    Plan {
        id: Uuid::new_v4().to_string(),
        project_id: project.id.clone(),
        created_at: now(),
        actions,
        local,
        remote,
        folders,
    }
}

pub fn select<'a>(
    plan: &'a Plan,
    project: &Project,
    ids: &[String],
    destructive_consent: bool,
) -> Result<Vec<&'a Action>> {
    if plan.project_id != project.id || now().saturating_sub(plan.created_at) > 900 {
        return Err(Error::Stale);
    }
    if ids.is_empty() {
        return Err(Error::Invalid("Choose at least one change.".into()));
    }
    let mut paths = BTreeSet::new();
    let mut selected = Vec::new();
    for id in ids {
        let action = plan
            .actions
            .iter()
            .find(|a| &a.id == id)
            .ok_or(Error::Stale)?;
        if !paths.insert(&action.path) {
            return Err(Error::Invalid(
                "Choose only one direction for each conflicting file.".into(),
            ));
        }
        if project.read_only && action.kind.writes_remote() {
            return Err(Error::Invalid("This project is read-only.".into()));
        }
        if action.kind.destructive() && !destructive_consent {
            return Err(Error::Invalid(
                "Review and confirm replacements and deletions first.".into(),
            ));
        }
        selected.push(action);
    }
    Ok(selected)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn entry(md5: &str) -> Entry {
        Entry {
            path: "part.step".into(),
            md5: md5.into(),
            size: 12,
            modified: "1".into(),
            remote_id: None,
            version: None,
            modified_by: None,
        }
    }
    fn tree(hash: Option<&str>) -> Tree {
        hash.map(|h| [("part.step".into(), entry(h))].into())
            .unwrap_or_default()
    }
    fn project(read_only: bool) -> Project {
        Project {
            id: "p".into(),
            name: "Project".into(),
            local_root: "/tmp/project".into(),
            drive_folder_id: "drive".into(),
            read_only,
        }
    }
    #[test]
    fn three_way_truth_table() {
        use ActionKind::*;
        let cases = [
            (None, Some("a"), None, vec![DownloadNew]),
            (Some("a"), None, None, vec![UploadNew]),
            (Some("a"), Some("a"), None, vec![]),
            (Some("a"), Some("b"), Some("a"), vec![DownloadRevision]),
            (Some("b"), Some("a"), Some("a"), vec![UploadRevision]),
            (
                Some("b"),
                Some("c"),
                Some("a"),
                vec![UploadConflict, DownloadConflict],
            ),
            (
                Some("b"),
                Some("c"),
                None,
                vec![UploadConflict, DownloadConflict],
            ),
            (Some("a"), None, Some("a"), vec![DeleteLocal]),
            (None, Some("a"), Some("a"), vec![DeleteCloud]),
            (Some("b"), None, Some("a"), vec![UploadNew]),
            (None, Some("b"), Some("a"), vec![DownloadNew]),
        ];
        for (l, r, b, expected) in cases {
            let baseline = b
                .map(|h| [("part.step".into(), h.into())].into())
                .unwrap_or_default();
            let p = plan(
                &project(false),
                tree(l),
                tree(r),
                &baseline,
                BTreeMap::new(),
            );
            assert_eq!(
                p.actions.iter().map(|a| a.kind).collect::<Vec<_>>(),
                expected,
                "{l:?}, {r:?}, {b:?}"
            );
        }
    }
    #[test]
    fn readonly_never_mutates_drive() {
        for l in [None, Some("a"), Some("b")] {
            for r in [None, Some("a"), Some("c")] {
                let p = plan(
                    &project(true),
                    tree(l),
                    tree(r),
                    &[("part.step".into(), "a".into())].into(),
                    BTreeMap::new(),
                );
                assert!(p.actions.iter().all(|a| !a.kind.writes_remote()));
            }
        }
    }
    #[test]
    fn conflict_choices_are_exclusive_and_require_consent() {
        let project = project(false);
        let p = plan(
            &project,
            tree(Some("a")),
            tree(Some("b")),
            &Baseline::new(),
            BTreeMap::new(),
        );
        let ids: Vec<_> = p.actions.iter().map(|a| a.id.clone()).collect();
        assert!(select(&p, &project, &ids, true).is_err());
        assert!(select(&p, &project, &ids[..1], false).is_err());
        assert_eq!(select(&p, &project, &ids[..1], true).unwrap().len(), 1);
        assert!(select(&p, &project, &["forged".into()], true).is_err());
    }
}
