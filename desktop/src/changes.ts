import type { Action, Entry } from "./types";

/** One displayed change, with the original removal retained for rename sync. */
export interface Change extends Action {
  removal?: Action;
}

export function projectFilePaths(
  local: Record<string, Entry>,
  remote: Record<string, Entry>,
  changes: Change[],
): string[] {
  const renamedPaths = new Set(
    changes.flatMap((change) => {
      if (!change.removal) return [];
      const expected = change.local ?? change.removal.local;
      const current = expected && local[expected.path];
      const missing = change.local ? change.removal.path : change.path;
      // Local notifications can arrive while a new Drive scan is still pending.
      // Collapse a source only while today's local listing supports the rename.
      const matches =
        current &&
        expected &&
        current.size === expected.size &&
        current.modified === expected.modified &&
        (!current.md5 || current.md5 === expected.md5);
      return matches && !local[missing] ? [change.removal.path] : [];
    }),
  );
  return [...new Set([...Object.keys(local), ...Object.keys(remote)])]
    .filter((path) => !renamedPaths.has(path))
    .sort();
}

export function groupRenames(actions: Action[]): Change[] {
  const candidates = new Map<string, { added: Action[]; removed: Action[] }>();
  for (const action of actions) {
    let entry;
    let direction;
    let added;
    switch (action.kind) {
      case "upload_new":
        if (action.remote) continue;
        entry = action.local;
        direction = "upload";
        added = true;
        break;
      case "delete_cloud":
        if (action.local) continue;
        entry = action.remote;
        direction = "upload";
        added = false;
        break;
      case "download_new":
        if (action.local) continue;
        entry = action.remote;
        direction = "download";
        added = true;
        break;
      case "delete_local":
        if (action.remote) continue;
        entry = action.local;
        direction = "download";
        added = false;
        break;
      default:
        continue;
    }
    if (!entry?.md5) continue;
    const key = JSON.stringify([direction, entry.md5, entry.size]);
    const group = candidates.get(key) ?? { added: [], removed: [] };
    group[added ? "added" : "removed"].push(action);
    candidates.set(key, group);
  }
  const pairs = new Map<string, Action>();
  const hidden = new Set<string>();
  for (const { added, removed } of candidates.values()) {
    // Identical copies can have several plausible matches. Never guess one.
    if (
      added.length !== 1 ||
      removed.length !== 1 ||
      added[0].path === removed[0].path
    )
      continue;
    pairs.set(added[0].id, removed[0]);
    hidden.add(removed[0].id);
  }
  return actions
    .filter((action) => !hidden.has(action.id))
    .map((action) => {
      const removal = pairs.get(action.id);
      return removal
        ? {
            ...action,
            removal,
            reason:
              action.kind === "upload_new"
                ? "Renamed on this computer; contents unchanged"
                : "Renamed in Google Drive; contents unchanged",
          }
        : action;
    });
}

export function syncActionIds(changes: Change[]): string[] {
  // Create the new copy before removing the old copy. Both operations still
  // use the backend's existing consent, stale-file checks, and recovery journal.
  return changes.flatMap((change) =>
    change.removal ? [change.id, change.removal.id] : [change.id],
  );
}

export function completedChanges(changes: Change[], completed: number): number {
  let count = 0;
  for (const change of changes) {
    completed -= change.removal ? 2 : 1;
    if (completed < 0) break;
    count += 1;
  }
  return count;
}

function fingerprint(action: Action | undefined): unknown {
  return (
    action && [
      action.path,
      action.kind,
      action.size,
      action.local,
      action.remote,
    ]
  );
}

function selectionKey(change: Change): string {
  return JSON.stringify([fingerprint(change), fingerprint(change.removal)]);
}

export function retainSelection(
  before: Action[],
  after: Action[],
  selected: Set<string>,
): Set<string> {
  const chosen = new Set(
    groupRenames(before)
      .filter((change) => selected.has(change.id))
      .map(selectionKey),
  );
  return new Set(
    groupRenames(after)
      .filter((next) => chosen.has(selectionKey(next)))
      .map((change) => change.id),
  );
}
