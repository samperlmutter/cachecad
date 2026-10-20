import type { Bootstrap, Entry, Plan, Project, Action } from "./types";
export const demoProjects: Project[] = [
  {
    id: "field-kit",
    name: "Field kit",
    localRoot: "/Users/demo/Documents/Permafrost/Field kit",
    driveFolderId: "demo-folder",
    readOnly: false,
  },
  {
    id: "orbital",
    name: "Orbital drive",
    localRoot: "/Users/demo/Documents/Permafrost/Orbital drive",
    driveFolderId: "demo-orbital",
    readOnly: false,
  },
  {
    id: "library",
    name: "Standard parts",
    localRoot: "/Users/demo/Documents/Permafrost/Standard parts",
    driveFolderId: "demo-library",
    readOnly: true,
  },
];
export function demoBootstrap(): Bootstrap {
  return {
    projects: demoProjects,
    settings: {
      workingDirectory: "/Users/demo/Documents/Permafrost",
      defaultDriveFolder: "",
      keepRevisionsForever: false,
      keepLocalBackups: true,
      closeToTray: true,
      checkUpdates: true,
    },
    account: {
      name: "Alex Morgan",
      email: "alex@example.test",
      subject: "demo",
    },
    googleConfigured: false,
    version: "2.0.0-alpha.1",
    activity: [],
    accountError: null,
    updaterConfigured: false,
  };
}
const files = [
  ["01 Assembly/Field kit.step", 2400000],
  ["02 Parts/Mounting bracket.stl", 128400],
  ["02 Parts/Top housing.sldprt", 1600000],
  ["02 Parts/Base plate.step", 860000],
  ["02 Parts/Spacer.stl", 48000],
  ["03 Drawings/Assembly drawing.pdf", 430000],
  ["03 Drawings/Cutting template.dxf", 64000],
  ["README.txt", 1800],
  ["02 Parts/Retired bracket.stl", 98000],
] as const;
export function demoPlan(projectId: string): Plan {
  const local: Record<string, Entry> = {};
  const remote: Record<string, Entry> = {};
  const actions: Action[] = [];
  files.forEach(([path, size], i) => {
    const e: Entry = {
      path,
      size,
      md5: `demo-${i}`,
      modified: "2026-10-19T14:20:00Z",
      remoteId: null,
      version: null,
      modifiedBy: "Alex Morgan",
    };
    if (i !== 3 && i !== 6) local[path] = e;
    if (i !== 1 && i !== 4)
      remote[path] = { ...e, remoteId: `remote-${i}`, version: "4" };
    const kinds =
      i === 0
        ? ["upload_conflict", "download_conflict"]
        : i === 1 || i === 4
          ? ["upload_new"]
          : i === 3 || i === 6
            ? ["download_new"]
            : i === 2
              ? ["upload_revision"]
              : i === 8
                ? ["delete_local"]
                : [];
    for (const kind of kinds)
      actions.push({
        id: `${i}-${kind}`,
        path,
        kind: kind as Action["kind"],
        reason:
          i === 0
            ? "Both copies changed since the last sync"
            : i === 8
              ? "Removed from Google Drive"
              : "Changes found since your last sync",
        size,
        local: local[path] ?? null,
        remote: remote[path] ?? null,
      });
  });
  const renamed = { ...local["README.txt"], path: "PROJECT.md" };
  delete local["README.txt"];
  local[renamed.path] = renamed;
  actions.push(
    {
      id: "readme-new",
      path: renamed.path,
      kind: "upload_new",
      reason: "Available on this computer",
      size: renamed.size,
      local: renamed,
      remote: null,
    },
    {
      id: "readme-old",
      path: "README.txt",
      kind: "delete_cloud",
      reason: "Deleted locally since the last sync",
      size: renamed.size,
      local: null,
      remote: remote["README.txt"],
    },
  );
  return {
    id: "demo-plan",
    projectId,
    createdAt: Date.now() / 1000,
    actions,
    local,
    remote,
    folders: { "": "demo-folder" },
  };
}
