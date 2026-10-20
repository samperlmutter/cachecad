import type { Action } from "./types";

/** One displayed change, with the original removal retained for rename sync. */
export interface Change extends Action {
  removal?: Action;
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

function unchanged(
  before: Action | undefined,
  after: Action | undefined,
): boolean {
  if (!before || !after) return before === after;
  return (
    before.path === after.path &&
    before.kind === after.kind &&
    before.size === after.size &&
    JSON.stringify(before.local) === JSON.stringify(after.local) &&
    JSON.stringify(before.remote) === JSON.stringify(after.remote)
  );
}

export function retainSelection(
  before: Action[],
  after: Action[],
  selected: Set<string>,
): Set<string> {
  const chosen = groupRenames(before).filter((change) =>
    selected.has(change.id),
  );
  return new Set(
    groupRenames(after)
      .filter((next) =>
        chosen.some(
          (old) => unchanged(old, next) && unchanged(old.removal, next.removal),
        ),
      )
      .map((change) => change.id),
  );
}
