export interface Project {
  id: string;
  name: string;
  localRoot: string;
  driveFolderId: string;
  readOnly: boolean;
}
export interface Entry {
  path: string;
  size: number;
  md5: string;
  modified: string;
  remoteId: string | null;
  version: string | null;
  modifiedBy: string | null;
}
export type ActionKind =
  | "upload_new"
  | "download_new"
  | "upload_revision"
  | "download_revision"
  | "delete_local"
  | "delete_cloud"
  | "upload_conflict"
  | "download_conflict";
export interface Action {
  id: string;
  path: string;
  kind: ActionKind;
  reason: string;
  size: number;
  local: Entry | null;
  remote: Entry | null;
}
export interface Plan {
  id: string;
  projectId: string;
  createdAt: number;
  actions: Action[];
  local: Record<string, Entry>;
  remote: Record<string, Entry>;
  folders: Record<string, string>;
}
export interface Settings {
  workingDirectory: string;
  defaultDriveFolder: string;
  keepRevisionsForever: boolean;
  keepLocalBackups: boolean;
  closeToTray: boolean;
  checkUpdates: boolean;
}
export interface Account {
  name: string;
  email: string;
  subject: string;
}
export interface Activity {
  id: string;
  projectId: string;
  path: string;
  kind: ActionKind;
  state: string;
  message: string;
  createdAt: number;
}
export interface Bootstrap {
  nativeCadSmoke?: boolean;
  projects: Project[];
  settings: Settings;
  account: Account | null;
  googleConfigured: boolean;
  version: string;
  activity: Activity[];
  accountError: string | null;
  previewCacheError?: string | null;
  updaterConfigured: boolean;
}
export interface Progress {
  projectId: string;
  path: string;
  completed: number;
  total: number;
  bytes: number;
  size: number;
  phase: string;
}
export interface Report {
  completed: number;
  failed: number;
  cancelled: boolean;
}
export const uploads = (kind: ActionKind) =>
  kind.startsWith("upload") || kind === "delete_cloud";
export const destructive = (kind: ActionKind) =>
  kind !== "upload_new" && kind !== "download_new";
export const conflict = (kind: ActionKind) => kind.endsWith("_conflict");
export const actionLabel: Record<ActionKind, string> = {
  upload_new: "Upload",
  download_new: "Download",
  upload_revision: "Upload revision",
  download_revision: "Download revision",
  delete_local: "Remove local",
  delete_cloud: "Move to Drive trash",
  upload_conflict: "Keep local copy",
  download_conflict: "Keep Drive copy",
};
export function formatBytes(bytes: number): string {
  if (bytes < 1000) return `${bytes} B`;
  const unit = Math.min(Math.floor(Math.log(bytes) / Math.log(1000)), 3);
  return `${(bytes / 1000 ** unit).toFixed(1)} ${["B", "KB", "MB", "GB"][unit]}`;
}
export function driveFolderId(input: string): string {
  const value = input.trim();
  if (/^[a-zA-Z0-9_-]+$/.test(value)) return value;
  try {
    const url = new URL(value);
    if (url.hostname === "drive.google.com")
      return url.pathname.match(/\/folders\/([a-zA-Z0-9_-]+)/)?.[1] ?? "";
  } catch {
    /* validation shown in form */
  }
  return "";
}
export function toggleSelection(
  selected: Set<string>,
  action: Action,
  actions: Action[],
): Set<string> {
  const next = new Set(selected);
  if (next.has(action.id)) next.delete(action.id);
  else {
    for (const other of actions)
      if (other.path === action.path) next.delete(other.id);
    next.add(action.id);
  }
  return next;
}
export const previewable = (path: string) =>
  /\.(stl|step|stp|igs|iges|brep|brp|obj|glb)$/i.test(path);
