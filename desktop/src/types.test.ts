import { describe, expect, it } from "vitest";
import {
  driveFolderId,
  toggleSelection,
  destructive,
  uploads,
  type Action,
} from "./types";
describe("Reviewing sync changes", () => {
  const local = {
    id: "up",
    path: "part.step",
    kind: "upload_conflict",
  } as Action;
  const remote = {
    id: "down",
    path: "part.step",
    kind: "download_conflict",
  } as Action;
  const other = {
    id: "other",
    path: "other.stl",
    kind: "upload_new",
  } as Action;
  it("switches a conflict direction without selecting both copies", () => {
    const selected = toggleSelection(new Set(["up", "other"]), remote, [
      local,
      remote,
      other,
    ]);
    expect([...selected]).toEqual(["other", "down"]);
    expect([
      ...toggleSelection(selected, remote, [local, remote, other]),
    ]).toEqual(["other"]);
  });
  it("requires review for every replacement, deletion and conflict", () => {
    expect(destructive("upload_new")).toBe(false);
    expect(destructive("download_new")).toBe(false);
    for (const kind of [
      "upload_revision",
      "download_revision",
      "delete_local",
      "delete_cloud",
      "upload_conflict",
      "download_conflict",
    ] as const)
      expect(destructive(kind)).toBe(true);
    expect(uploads("delete_cloud")).toBe(true);
    expect(uploads("delete_local")).toBe(false);
  });
  it("accepts Google folder links and IDs, rejecting foreign domains", () => {
    expect(
      driveFolderId(
        "https://drive.google.com/drive/u/0/folders/folder-123?usp=sharing",
      ),
    ).toBe("folder-123");
    expect(driveFolderId("folder_123")).toBe("folder_123");
    expect(driveFolderId("https://evil.test/folders/folder-123")).toBe("");
    expect(
      driveFolderId("https://drive.google.com.evil.test/folders/123"),
    ).toBe("");
  });
});
