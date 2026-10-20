import { describe, expect, it } from "vitest";
import {
  groupRenames,
  syncActionIds,
  completedChanges,
  retainSelection,
  projectFilePaths,
} from "./changes";
import {
  toggleSelection,
  type Action,
  type ActionKind,
  type Entry,
} from "./types";

function action(
  id: string,
  kind: ActionKind,
  path: string,
  md5 = "same-content",
  size = 12,
): Action {
  const entry: Entry = {
    path,
    md5,
    size,
    modified: "1",
    remoteId: null,
    version: null,
    modifiedBy: null,
  };
  return {
    id,
    kind,
    path,
    size,
    reason: "Change detected",
    local: kind === "download_new" || kind === "delete_cloud" ? null : entry,
    remote:
      kind === "upload_new" || kind === "delete_local"
        ? null
        : { ...entry, remoteId: `remote-${path}`, version: "1" },
  };
}

describe("Displaying unchanged-content renames", () => {
  it("keeps a restored local source visible while an older rename plan is displayed", () => {
    const added = action("add", "upload_new", "new.step");
    const removed = action("remove", "delete_cloud", "old.step");
    const changes = groupRenames([added, removed]);
    const remote = { "old.step": removed.remote! };
    expect(
      projectFilePaths({ "new.step": added.local! }, remote, changes),
    ).toEqual(["new.step"]);
    const restored = { ...added.local!, path: "old.step", modified: "2" };
    expect(projectFilePaths({ "old.step": restored }, remote, changes)).toEqual(
      ["old.step"],
    );
  });

  it("keeps a locally edited source visible when a pending Drive rename is stale", () => {
    const added = action("add", "download_new", "new.step");
    const removed = action("remove", "delete_local", "old.step");
    const changes = groupRenames([added, removed]);
    const remote = { "new.step": added.remote! };
    expect(
      projectFilePaths({ "old.step": removed.local! }, remote, changes),
    ).toEqual(["new.step"]);
    const edited = { ...removed.local!, modified: "2" };
    expect(projectFilePaths({ "old.step": edited }, remote, changes)).toEqual([
      "new.step",
      "old.step",
    ]);
  });
  it.each([
    ["upload_new", "delete_cloud"],
    ["download_new", "delete_local"],
  ] as const)(
    "pairs %s with %s into one selectable rename",
    (added, removed) => {
      const old = action("remove", removed, "old.step");
      const next = action("add", added, "parts/new.step");
      const changes = groupRenames([old, next]);
      expect(changes).toHaveLength(1);
      expect(changes[0].path).toBe("parts/new.step");
      expect(changes[0].removal?.path).toBe("old.step");
      const selected = toggleSelection(new Set(), changes[0], changes);
      expect(syncActionIds(changes.filter((c) => selected.has(c.id)))).toEqual([
        "add",
        "remove",
      ]);
      expect(toggleSelection(selected, changes[0], changes).size).toBe(0);
    },
  );

  it("leaves renamed-and-edited files as separate additions and removals", () => {
    const changes = groupRenames([
      action("remove", "delete_cloud", "old.step"),
      action("add", "upload_new", "new.step", "edited-content"),
    ]);
    expect(changes.map((c) => c.id)).toEqual(["remove", "add"]);
    expect(changes.every((c) => !c.removal)).toBe(true);
  });

  it("does not infer a rename from missing hashes, differing sizes, or opposite directions", () => {
    for (const [removed, added] of [
      [
        action("remove", "delete_cloud", "old.step", ""),
        action("add", "upload_new", "new.step", ""),
      ],
      [
        action("remove", "delete_cloud", "old.step"),
        action("add", "upload_new", "new.step", "same-content", 13),
      ],
      [
        action("remove", "delete_cloud", "old.step"),
        action("add", "download_new", "new.step"),
      ],
    ]) {
      const changes = groupRenames([removed, added]);
      expect(changes.map((c) => c.id)).toEqual(["remove", "add"]);
      expect(changes.every((c) => !c.removal)).toBe(true);
    }
  });

  it("does not guess which identical-content file was renamed", () => {
    const removed = action("remove", "delete_cloud", "old.step");
    const added = action("add", "upload_new", "new.step");
    for (const actions of [
      [removed, added, action("copy", "upload_new", "copy.step")],
      [removed, added, action("other", "delete_cloud", "other.step")],
    ]) {
      const changes = groupRenames(actions);
      expect(changes.map((c) => c.id)).toEqual(actions.map((a) => a.id));
      expect(changes.every((c) => !c.removal)).toBe(true);
    }
  });

  it("keeps unrelated modifications and conflicts unchanged", () => {
    const revision = action("revision", "upload_revision", "edited.step");
    const conflict = action("conflict", "download_conflict", "conflict.step");
    const changes = groupRenames([
      revision,
      action("remove", "delete_local", "old.step"),
      action("add", "download_new", "new.step"),
      conflict,
    ]);
    expect(changes.map((c) => c.id)).toEqual(["revision", "add", "conflict"]);
    expect(changes[0]).toEqual(revision);
    expect(changes[2]).toEqual(conflict);
  });

  it("counts a rename as completed only when both sync operations finish", () => {
    const changes = groupRenames([
      action("add", "upload_new", "new.step"),
      action("remove", "delete_cloud", "old.step"),
      action("other", "download_new", "other.step", "other-content"),
    ]);
    expect(syncActionIds(changes)).toEqual(["add", "remove", "other"]);
    expect(completedChanges(changes, 0)).toBe(0);
    expect(completedChanges(changes, 1)).toBe(0);
    expect(completedChanges(changes, 2)).toBe(1);
    expect(completedChanges(changes, 3)).toBe(2);
  });

  it("preserves rename selection across rescans only while both copies match", () => {
    const before = [
      action("old-add", "upload_new", "new.step"),
      action("old-remove", "delete_cloud", "old.step"),
    ];
    const after = [
      action("new-add", "upload_new", "new.step"),
      action("new-remove", "delete_cloud", "old.step"),
    ];
    expect([...retainSelection(before, after, new Set(["old-add"]))]).toEqual([
      "new-add",
    ]);
    const movedSource = [
      after[0],
      action("remove", "delete_cloud", "another.step"),
    ];
    expect(
      retainSelection(before, movedSource, new Set(["old-add"])).size,
    ).toBe(0);
    const editedSource = [
      after[0],
      action("remove", "delete_cloud", "old.step", "edited"),
    ];
    expect(
      retainSelection(before, editedSource, new Set(["old-add"])).size,
    ).toBe(0);
    const changedVersion = [
      after[0],
      {
        ...after[1],
        remote: { ...after[1].remote!, version: "2" },
      },
    ];
    expect(
      retainSelection(before, changedVersion, new Set(["old-add"])).size,
    ).toBe(0);
  });
});
