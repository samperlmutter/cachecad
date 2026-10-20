import { invoke, isTauri } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import type {
  Bootstrap,
  Plan,
  Project,
  Settings,
  Account,
  Report,
  Activity,
  Entry,
} from "./types";
import { demoBootstrap, demoPlan } from "./demo";
export const demo =
  import.meta.env.DEV && new URLSearchParams(location.search).has("demo");
let sample = demoBootstrap();
export const native = isTauri();
async function command<T>(
  name: string,
  args?: Record<string, unknown>,
): Promise<T> {
  if (native) return invoke<T>(name, args);
  if (!demo)
    throw new Error(
      "Open Permafrost as a desktop application to connect your folders. Use the demo workspace to explore the interface.",
    );
  switch (name) {
    case "bootstrap":
      return structuredClone(sample) as T;
    case "list_local_files":
      return demoPlan(String(args?.projectId)).local as T;
    case "scan_project":
      return demoPlan(String(args?.projectId)) as T;
    case "save_project": {
      const project = args?.project as Project;
      sample.projects = [
        ...sample.projects.filter((p) => p.id !== project.id),
        project,
      ];
      return undefined as T;
    }
    case "remove_project":
      sample.projects = sample.projects.filter((p) => p.id !== args?.projectId);
      return undefined as T;
    case "save_settings":
      sample.settings = args?.settings as Settings;
      return undefined as T;
    case "activity":
      return [] as T;
    case "cancel_operation":
      return undefined as T;
    case "reorder_projects":
      sample.projects.sort(
        (a, b) =>
          (args?.ids as string[]).indexOf(a.id) -
          (args?.ids as string[]).indexOf(b.id),
      );
      return undefined as T;
    default:
      throw new Error(
        "This action uses the native desktop application. The demo does not change files or access Google Drive.",
      );
  }
}
export const api = {
  bootstrap: () => command<Bootstrap>("bootstrap"),
  localFiles: (projectId: string) =>
    command<Record<string, Entry>>("list_local_files", { projectId }),
  scan: (projectId: string) => command<Plan>("scan_project", { projectId }),
  saveProject: (project: Project) => command<void>("save_project", { project }),
  removeProject: (projectId: string) =>
    command<void>("remove_project", { projectId }),
  reorder: (ids: string[]) => command<void>("reorder_projects", { ids }),
  saveSettings: (settings: Settings) =>
    command<void>("save_settings", { settings }),
  signIn: () => command<Account>("sign_in"),
  signOut: (revoke = false) => command<void>("sign_out", { revoke }),
  sync: (
    projectId: string,
    planId: string,
    actionIds: string[],
    consent: boolean,
  ) =>
    command<Report>("execute_sync", { projectId, planId, actionIds, consent }),
  cancel: () => command<void>("cancel_operation"),
  activity: () => command<Activity[]>("activity"),
  resolve: (operationId: string) =>
    command<void>("resolve_operation", { operationId }),
  reveal: (projectId: string) => command<void>("reveal_project", { projectId }),
  openDrive: (projectId: string) => command<void>("open_drive", { projectId }),
  preview: (projectId: string, path: string) =>
    command<ArrayBuffer>("read_preview", { projectId, path }),
  exportDiagnostics: () => command<string>("export_diagnostics"),
  chooseDirectory: async (): Promise<string | null> =>
    demo
      ? "/Users/demo/Documents/Permafrost/New project"
      : ((await open({
          directory: true,
          multiple: false,
          title: "Choose a local project folder",
        })) as string | null),
};
