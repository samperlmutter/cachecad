import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  ArrowDown,
  ArrowDownLeft,
  ArrowRight,
  ArrowUp,
  ArrowUpRight,
  Bell,
  Boxes,
  Check,
  CheckCheck,
  ChevronDown,
  ChevronRight,
  CircleCheck,
  CircleHelp,
  Cloud,
  CloudOff,
  ExternalLink,
  File,
  FileBox,
  Folder,
  FolderOpen,
  FolderPlus,
  HardDrive,
  History,
  LayoutGrid,
  LoaderCircle,
  LockKeyhole,
  LogOut,
  MoreHorizontal,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  Snowflake,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  TriangleAlert,
  X,
} from "lucide-react";
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import type { Update } from "@tauri-apps/plugin-updater";
import { api, demo, native } from "./api";
import {
  actionLabel,
  conflict,
  destructive,
  driveFolderId,
  formatBytes,
  toggleSelection,
  uploads,
  type Action,
  type Activity,
  type Bootstrap,
  type Plan,
  type Progress,
  type Project,
  type Settings,
} from "./types";
import Dialog from "./components/Dialog";
const CadPreview = lazy(() => import("./components/CadPreview"));
type Screen = "workspace" | "activity" | "settings";
type Filter = "all" | "upload" | "download" | "conflict";
const iconSize = 17;
function GoogleDrive({ size = 20 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <path d="M8 2h8l8 14h-8L8 2Z" fill="#13c8eb" />
      <path d="M8 2 0 16l4 7 8-14-4-7Z" fill="#8c969a" />
      <path d="M4 23h16l4-7H8l-4 7Z" fill="#343c40" />
    </svg>
  );
}
function FileIcon({ path }: { path: string }) {
  const ext = path.split(".").pop()?.toLowerCase();
  return (
    <span
      className={`file-icon ${ext === "pdf" ? "pdf" : ext === "sldprt" || ext === "sldasm" ? "solidworks" : ""}`}
    >
      {[
        "step",
        "stp",
        "stl",
        "igs",
        "iges",
        "brep",
        "obj",
        "glb",
        "sldprt",
        "sldasm",
      ].includes(ext ?? "") ? (
        <FileBox size={20} />
      ) : (
        <File size={19} />
      )}
    </span>
  );
}
function statusOf(actions: Action[]) {
  if (actions.some((a) => conflict(a.kind))) return "conflict";
  if (actions.length) return "changes";
  return "synced";
}

export default function App() {
  const [data, setData] = useState<Bootstrap>();
  const [projectId, setProjectId] = useState("");
  const [screen, setScreen] = useState<Screen>("workspace");
  const [plan, setPlan] = useState<Plan>();
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(new Set<string>());
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<"changes" | "files">("changes");
  const [preview, setPreview] = useState("");
  const [progress, setProgress] = useState<Progress>();
  const [modal, setModal] = useState<
    "project" | "review" | "remove" | "disconnect" | null
  >(null);
  const [activity, setActivity] = useState<Activity[]>([]);
  const [update, setUpdate] = useState<Update | null>(null);
  const [updateMessage, setUpdateMessage] = useState("");
  const mounted = useRef(true);
  const project = data?.projects.find((p) => p.id === projectId);
  const actions = plan?.projectId === projectId ? plan.actions : [];
  const selectedActions = actions.filter((a) => selected.has(a.id));
  const run = useCallback(
    async <T,>(
      label: string,
      task: () => Promise<T>,
    ): Promise<T | undefined> => {
      setBusy(label);
      setError("");
      setMessage("");
      try {
        return await task();
      } catch (e) {
        setError(String(e instanceof Error ? e.message : e));
        return undefined;
      } finally {
        if (mounted.current) setBusy("");
      }
    },
    [],
  );
  useEffect(() => {
    mounted.current = true;
    void api
      .bootstrap()
      .then((result) => {
        if (mounted.current) {
          setData(result);
          setActivity(result.activity);
          setProjectId(result.projects[0]?.id ?? "");
          if (result.accountError) setError(result.accountError);
        }
      })
      .catch((e) => setError(String(e)));
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    if (!native) return;
    let stop: (() => void) | undefined;
    let closed = false;
    void listen<Progress>("sync-progress", (event) =>
      setProgress(event.payload),
    ).then((fn) => {
      if (closed) fn();
      else stop = fn;
    });
    return () => {
      closed = true;
      stop?.();
    };
  }, []);
  const scan = useCallback(
    async (id: string) => {
      setPlan(undefined);
      setSelected(new Set());
      setProgress(undefined);
      await run("Scanning folders", async () => {
        const result = await api.scan(id);
        setPlan(result);
        if (demo) setPreview("02 Parts/Mounting bracket.stl");
      });
    },
    [run],
  );
  useEffect(() => {
    setPreview("");
    setFilter("all");
    setQuery("");
    if (projectId && data?.account) void scan(projectId);
    else setPlan(undefined);
  }, [projectId, data?.account?.subject, scan]);
  const checkUpdates = useCallback(async () => {
    if (!data?.updaterConfigured) {
      setUpdateMessage(
        "Signed updates are not configured in this development build.",
      );
      return;
    }
    try {
      setUpdateMessage("Checking for updates…");
      const { check } = await import("@tauri-apps/plugin-updater");
      const result = await check();
      setUpdate(result);
      setUpdateMessage(
        result
          ? `Version ${result.version} is ready to download.`
          : "You’re running the latest version.",
      );
    } catch {
      setUpdateMessage(
        "Could not check for updates. Check your connection and try again.",
      );
    }
  }, [data?.updaterConfigured]);
  useEffect(() => {
    if (!native || !data || data.projects.length) return;
    const timer = setInterval(() => {
      const canvas = document.querySelector<HTMLCanvasElement>(
        "canvas[data-preview-ready]",
      );
      if (!canvas) return;
      clearInterval(timer);
      void invoke("native_ready", {
        evidence: {
          workspaceVisible: !!document.querySelector(".sidebar .brand"),
          modelRendered: true,
          canvasWidth: canvas.width,
          canvasHeight: canvas.height,
          title: document.title,
        },
      }).catch((e) => setError(String(e)));
    }, 300);
    return () => clearInterval(timer);
  }, [data]);
  useEffect(() => {
    if (!native) return;
    const stop = listen("quit-deferred", () =>
      setMessage(
        "Cancelling the current operation safely. Wait for it to settle, then quit again.",
      ),
    );
    return () => {
      void stop.then((fn) => fn());
    };
  }, []);
  const checkedStartup = useRef(false);
  useEffect(() => {
    if (
      data?.settings.checkUpdates &&
      data.updaterConfigured &&
      !checkedStartup.current
    ) {
      checkedStartup.current = true;
      void checkUpdates();
    }
  }, [data, checkUpdates]);
  const installUpdate = () =>
    void run("Installing update", async () => {
      if (!update) return;
      await update.downloadAndInstall((event) => {
        if (event.event === "Started")
          setUpdateMessage("Downloading the signed update…");
        if (event.event === "Finished") setUpdateMessage("Installing update…");
      });
      const { relaunch } = await import("@tauri-apps/plugin-process");
      await relaunch();
    });
  const refreshActivity = async () => {
    const result = await api.activity();
    setActivity(result);
  };
  const navigate = (next: Screen) => {
    setScreen(next);
    if (next === "activity") void run("Loading activity", refreshActivity);
  };
  const doSync = async () => {
    if (!project || !plan) return;
    setModal(null);
    setProgress(undefined);
    await run("Syncing selected changes", async () => {
      const report = await api.sync(project.id, plan.id, [...selected], true);
      await refreshActivity();
      setMessage(
        report.cancelled
          ? "Sync cancelled. Completed changes are saved."
          : `${report.completed} ${report.completed === 1 ? "change" : "changes"} synced${report.failed ? ` · ${report.failed} failed — review Activity before retrying` : ""}.`,
      );
      setPlan(undefined);
      setSelected(new Set());
    });
    setProgress(undefined);
  };
  const filePaths = useMemo(
    () =>
      [
        ...new Set([
          ...Object.keys(plan?.local ?? {}),
          ...Object.keys(plan?.remote ?? {}),
        ]),
      ].sort(),
    [plan],
  );
  const rows = useMemo(
    () =>
      (tab === "files"
        ? filePaths
        : [...new Set(actions.map((a) => a.path))]
      ).filter((path) => {
        if (!path.toLowerCase().includes(query.toLowerCase())) return false;
        const at = actions.filter((a) => a.path === path);
        return (
          tab === "files" ||
          filter === "all" ||
          (filter === "conflict"
            ? at.some((a) => conflict(a.kind))
            : at.some((a) =>
                filter === "upload" ? uploads(a.kind) : !uploads(a.kind),
              ))
        );
      }),
    [tab, filePaths, actions, query, filter],
  );
  const uploadCount = actions.filter(
    (a) => uploads(a.kind) && !conflict(a.kind),
  ).length;
  const downloadCount = actions.filter(
    (a) => !uploads(a.kind) && !conflict(a.kind),
  ).length;
  const conflictCount = new Set(
    actions.filter((a) => conflict(a.kind)).map((a) => a.path),
  ).size;
  const loadPreview = useMemo(
    () =>
      project && preview ? () => api.preview(project.id, preview) : undefined,
    [project?.id, preview],
  );
  const allSelectable = actions.filter((a) => !conflict(a.kind));
  const selectAll = () =>
    setSelected(
      allSelectable.every((a) => selected.has(a.id))
        ? new Set()
        : new Set(allSelectable.map((a) => a.id)),
    );
  const startupError = !data && error;
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button
          className="brand"
          onClick={() => navigate("workspace")}
          aria-label="Permafrost home"
        >
          <span className="brand-symbol">
            <Snowflake size={25} strokeWidth={1.6} />
          </span>
          <span>
            permafrost<small>YOUR WORK, CONNECTED</small>
          </span>
        </button>
        <div className="workspace-picker">
          <span className="workspace-avatar">
            <Boxes size={19} />
          </span>
          <div>
            <strong>My workspace</strong>
            <small>Local + Google Drive</small>
          </div>
          <ChevronDown size={14} />
        </div>
        <div className="sidebar-label">WORKSPACE</div>
        <nav aria-label="Main navigation">
          <button
            className={`nav-item ${screen === "workspace" ? "active" : ""}`}
            onClick={() => navigate("workspace")}
          >
            <LayoutGrid size={iconSize} />
            Projects
            <span className="nav-count">{data?.projects.length ?? 0}</span>
          </button>
          <button
            className={`nav-item ${screen === "activity" ? "active" : ""}`}
            onClick={() => navigate("activity")}
          >
            <History size={iconSize} />
            Activity
            {activity.some((a) => a.state === "uncertain") && (
              <span className="attention-dot" />
            )}
          </button>
        </nav>
        <div className="sidebar-label project-label">
          <span>YOUR PROJECTS</span>
          <button
            className="icon-button"
            aria-label="Add project"
            onClick={() => setModal("project")}
            disabled={!!busy}
          >
            <Plus size={16} />
          </button>
        </div>
        <nav className="project-nav" aria-label="Projects">
          {data?.projects.map((p) => (
            <button
              key={p.id}
              className={`project-item ${p.id === projectId && screen === "workspace" ? "selected" : ""}`}
              disabled={!!busy}
              onClick={() => {
                setProjectId(p.id);
                setScreen("workspace");
              }}
            >
              <Folder size={17} />
              <span>{p.name}</span>
              {p.readOnly ? (
                <LockKeyhole size={12} />
              ) : (
                <span
                  className={`project-dot ${p.id === projectId && actions.length ? "pending" : ""}`}
                />
              )}
            </button>
          ))}
          {!data?.projects.length && (
            <p className="sidebar-empty">Connect a folder to get started.</p>
          )}
        </nav>
        <div className="sidebar-bottom">
          <div className="connection-card">
            <span
              className={`connection-dot ${data?.account ? "online" : ""}`}
            />
            <div>
              <strong>
                {data?.account ? "Drive connected" : "Drive not connected"}
              </strong>
              <small>
                {data?.account
                  ? "Ready when you are"
                  : "Sign in to start syncing"}
              </small>
            </div>
            {data?.account ? <Cloud size={16} /> : <CloudOff size={16} />}
          </div>
          <button
            className={`nav-item ${screen === "settings" ? "active" : ""}`}
            onClick={() => navigate("settings")}
          >
            <Settings2 size={iconSize} />
            Settings
          </button>
          <button
            className="nav-item"
            onClick={() => {
              setScreen("settings");
              setMessage(
                "Diagnostics stay on your device. Export a report below to review it before sharing.",
              );
            }}
          >
            <CircleHelp size={iconSize} />
            Help & diagnostics
            <ExternalLink size={13} className="push-right" />
          </button>
          <div className="account-row">
            <span className="avatar">
              {data?.account?.name.charAt(0) ?? "Y"}
            </span>
            <div>
              <strong>{data?.account?.name ?? "Your workspace"}</strong>
              <small>
                {demo
                  ? "Demo · sample data"
                  : (data?.account?.email ?? "Stored on this device")}
              </small>
            </div>
            {data?.account ? (
              <button
                className="icon-button"
                aria-label="Sign out"
                disabled={!!busy}
                onClick={() =>
                  void run("Signing out", async () => {
                    await api.signOut();
                    setData((d) => (d ? { ...d, account: null } : d));
                  })
                }
              >
                <LogOut size={15} />
              </button>
            ) : null}
          </div>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <div className="breadcrumbs">
            <span>Workspace</span>
            <ChevronRight size={13} />
            <strong>
              {screen === "workspace"
                ? (project?.name ?? "Projects")
                : screen === "activity"
                  ? "Activity"
                  : "Settings"}
            </strong>
          </div>
          <div className="topbar-right">
            {demo && <span className="demo-badge">DEMO WORKSPACE</span>}
            <span className="local-badge">
              <ShieldCheck size={13} />
              Private by design
            </span>
            <button
              className="icon-button"
              aria-label="View activity"
              onClick={() => navigate("activity")}
            >
              <Bell size={18} />
            </button>
          </div>
        </header>
        <div className="content">
          {(error || message || update) && (
            <div
              className={`notice ${error ? "error" : update ? "update" : "success"}`}
              role={error ? "alert" : "status"}
            >
              {error ? (
                <TriangleAlert size={17} />
              ) : update ? (
                <Sparkles size={17} />
              ) : (
                <CircleCheck size={17} />
              )}
              <span>
                {error ||
                  message ||
                  `Permafrost ${update?.version} is available.`}
              </span>
              {update && !error && !message && (
                <button
                  className="text-button"
                  onClick={installUpdate}
                  disabled={!!busy}
                >
                  Update now
                  <ArrowRight size={14} />
                </button>
              )}
              <button
                className="icon-button"
                aria-label="Dismiss message"
                onClick={() => {
                  setError("");
                  setMessage("");
                  if (update) setUpdate(null);
                }}
              >
                <X size={15} />
              </button>
            </div>
          )}
          {!data ? (
            <div className="empty-state">
              {startupError ? (
                <>
                  <TriangleAlert />
                  <h1>Couldn’t open your workspace</h1>
                  <p>{error}</p>
                  <button className="primary" onClick={() => location.reload()}>
                    Try again
                  </button>
                </>
              ) : (
                <>
                  <LoaderCircle className="spin" />
                  <p>Opening your workspace…</p>
                </>
              )}
            </div>
          ) : screen === "settings" ? (
            <SettingsPage
              data={data}
              busy={busy}
              updateMessage={updateMessage}
              checkUpdates={() => void checkUpdates()}
              installUpdate={update ? installUpdate : undefined}
              onSave={(settings) =>
                void run("Saving preferences", async () => {
                  await api.saveSettings(settings);
                  setData({ ...data, settings });
                  setMessage("Preferences saved.");
                })
              }
              onSignIn={() =>
                void run("Waiting for Google sign-in", async () => {
                  const account = await api.signIn();
                  setData({ ...data, account });
                })
              }
              onDisconnect={() => setModal("disconnect")}
              onExport={() =>
                void run("Exporting diagnostics", async () => {
                  const path = await api.exportDiagnostics();
                  setMessage(`Diagnostics saved to ${path}`);
                })
              }
              onReorder={(ids) =>
                void run("Reordering projects", async () => {
                  await api.reorder(ids);
                  const next = await api.bootstrap();
                  setData(next);
                })
              }
            />
          ) : screen === "activity" ? (
            <ActivityPage
              activity={activity}
              projects={data.projects}
              busy={busy}
              onRefresh={() => void run("Loading activity", refreshActivity)}
              onResolve={(id) =>
                void run("Resolving interrupted transfer", async () => {
                  await api.resolve(id);
                  await refreshActivity();
                  setPlan(undefined);
                  setSelected(new Set());
                  setMessage(
                    "Transfer marked reviewed. Scan again before syncing.",
                  );
                })
              }
            />
          ) : !project ? (
            <div className="welcome">
              <div className="welcome-copy">
                <span className="eyebrow">A CLEARER WORKSPACE</span>
                <h1>
                  Your next great
                  <br />
                  idea, in sync.
                </h1>
                <p>
                  Bring your local CAD files and Google Drive together. See what
                  changed, preview your models, and sync with confidence.
                </p>
                <button className="primary" onClick={() => setModal("project")}>
                  <FolderPlus size={18} />
                  Connect your first folder
                  <ArrowRight size={16} />
                </button>
                <div className="welcome-features">
                  <span>
                    <Check size={14} />
                    Review every change
                  </span>
                  <span>
                    <Check size={14} />
                    Preview in 3D
                  </span>
                  <span>
                    <Check size={14} />
                    Keep local backups
                  </span>
                </div>
              </div>
              <div className="welcome-model">
                <Suspense>
                  <CadPreview path="A little room for your next idea" sample />
                </Suspense>
              </div>
            </div>
          ) : (
            <>
              <div className="page-heading">
                <div>
                  <div className="eyebrow">
                    <span className="tiny-dot" />
                    PROJECT WORKSPACE
                  </div>
                  <h1>
                    {project.name}
                    {project.readOnly && (
                      <span className="readonly-badge">
                        <LockKeyhole size={12} />
                        Read-only
                      </span>
                    )}
                  </h1>
                  <p>Your local files and Google Drive, on the same page.</p>
                </div>
                <div className="heading-actions">
                  <button
                    className="secondary"
                    disabled={!!busy}
                    onClick={() =>
                      void run("Opening folder", () => api.reveal(project.id))
                    }
                  >
                    <FolderOpen size={16} />
                    Open folder
                    <ExternalLink size={12} />
                  </button>
                  <button
                    className="primary"
                    disabled={!!busy || !data.account}
                    onClick={() => void scan(project.id)}
                  >
                    <RefreshCw
                      size={16}
                      className={busy === "Scanning folders" ? "spin" : ""}
                    />
                    {busy === "Scanning folders"
                      ? "Scanning…"
                      : "Scan for changes"}
                  </button>
                  <button
                    className="icon-button more-button"
                    aria-label="Project options"
                    disabled={!!busy}
                    onClick={() => setModal("remove")}
                  >
                    <MoreHorizontal size={20} />
                  </button>
                </div>
              </div>
              <div className="folder-connection">
                <div>
                  <span className="folder-marker">
                    <HardDrive size={19} />
                  </span>
                  <div>
                    <span className="small-label">LOCAL FOLDER</span>
                    <button
                      className="path-button"
                      onClick={() =>
                        void run("Opening folder", () => api.reveal(project.id))
                      }
                    >
                      {project.localRoot}
                      <ExternalLink size={12} />
                    </button>
                  </div>
                </div>
                <div className="connection-link">
                  <span />
                  <RefreshCw size={16} />
                  <span />
                </div>
                <div>
                  <span className="folder-marker drive">
                    <GoogleDrive size={20} />
                  </span>
                  <div>
                    <span className="small-label">GOOGLE DRIVE</span>
                    <button
                      className="path-button"
                      onClick={() =>
                        void run("Opening Drive", () =>
                          api.openDrive(project.id),
                        )
                      }
                    >
                      {project.name}
                      <ExternalLink size={12} />
                    </button>
                  </div>
                  <span className="connected-tag">
                    {data.account ? "Connected" : "Sign-in required"}
                  </span>
                </div>
              </div>
              {!data.account && (
                <div className="sign-in-callout">
                  <GoogleDrive size={24} />
                  <div>
                    <strong>Connect to Google Drive</strong>
                    <p>
                      Sign in in your browser to compare your project folders.
                    </p>
                  </div>
                  <button
                    className="primary"
                    disabled={!!busy}
                    onClick={() =>
                      void run("Waiting for Google sign-in", async () => {
                        const account = await api.signIn();
                        setData({ ...data, account });
                      })
                    }
                  >
                    Sign in with Google
                    <ArrowRight size={15} />
                  </button>
                </div>
              )}
              <div className="summary-grid">
                <button
                  className={`summary-card ${filter === "upload" ? "selected" : ""}`}
                  onClick={() => {
                    setFilter(filter === "upload" ? "all" : "upload");
                    setTab("changes");
                  }}
                >
                  <span className="summary-icon upload">
                    <ArrowUpRight size={23} />
                  </span>
                  <div>
                    <span className="small-label">TO GOOGLE DRIVE</span>
                    <strong>
                      {uploadCount}
                      <small>
                        {" "}
                        {uploadCount === 1 ? "change" : "changes"} to upload
                      </small>
                    </strong>
                    <span>Bring your latest work along</span>
                  </div>
                  <ChevronRight size={16} />
                </button>
                <button
                  className={`summary-card ${filter === "download" ? "selected" : ""}`}
                  onClick={() => {
                    setFilter(filter === "download" ? "all" : "download");
                    setTab("changes");
                  }}
                >
                  <span className="summary-icon download">
                    <ArrowDownLeft size={23} />
                  </span>
                  <div>
                    <span className="small-label">TO THIS COMPUTER</span>
                    <strong>
                      {downloadCount}
                      <small>
                        {" "}
                        {downloadCount === 1 ? "change" : "changes"} to download
                      </small>
                    </strong>
                    <span>Pick up where your team left off</span>
                  </div>
                  <ChevronRight size={16} />
                </button>
                <button
                  className={`summary-card ${filter === "conflict" ? "selected" : ""}`}
                  onClick={() => {
                    setFilter(filter === "conflict" ? "all" : "conflict");
                    setTab("changes");
                  }}
                >
                  <span
                    className={`summary-icon ${conflictCount ? "conflict" : "clear"}`}
                  >
                    {conflictCount ? (
                      <SlidersHorizontal size={21} />
                    ) : (
                      <CheckCheck size={23} />
                    )}
                  </span>
                  <div>
                    <span className="small-label">NEEDS YOUR ATTENTION</span>
                    <strong>
                      {conflictCount}
                      <small>
                        {" "}
                        {conflictCount === 1 ? "conflict" : "conflicts"} to
                        review
                      </small>
                    </strong>
                    <span>
                      {conflictCount
                        ? "Choose the copy you want to keep"
                        : "Everything has a clear direction"}
                    </span>
                  </div>
                  <ChevronRight size={16} />
                </button>
              </div>
              <div className="workspace-section-heading">
                <div className="tab-list" role="tablist" aria-label="File view">
                  <button
                    role="tab"
                    aria-selected={tab === "changes"}
                    className={tab === "changes" ? "active" : ""}
                    onClick={() => setTab("changes")}
                  >
                    Changes
                    <span>{new Set(actions.map((a) => a.path)).size}</span>
                  </button>
                  <button
                    role="tab"
                    aria-selected={tab === "files"}
                    className={tab === "files" ? "active" : ""}
                    onClick={() => setTab("files")}
                  >
                    All files<span>{filePaths.length}</span>
                  </button>
                </div>
                <span className="scan-time">
                  {plan ? (
                    <>
                      <CircleCheck size={13} />
                      Scanned{" "}
                      {new Date(plan.createdAt * 1000).toLocaleTimeString([], {
                        hour: "numeric",
                        minute: "2-digit",
                      })}
                    </>
                  ) : busy === "Scanning folders" ? (
                    "Comparing both folders…"
                  ) : (
                    "Scan to see the latest files"
                  )}
                </span>
              </div>
              <div
                className={`file-workspace ${preview ? "with-preview" : ""}`}
              >
                <section className="files-panel" aria-label="Project files">
                  <div className="files-toolbar">
                    <label className="search">
                      <Search size={16} />
                      <input
                        placeholder="Find a file…"
                        aria-label="Find a file"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                      />
                      {query && (
                        <button
                          className="icon-button"
                          aria-label="Clear search"
                          onClick={() => setQuery("")}
                        >
                          <X size={13} />
                        </button>
                      )}
                    </label>
                    <select
                      aria-label="Filter changes"
                      value={filter}
                      onChange={(e) => setFilter(e.target.value as Filter)}
                      disabled={tab === "files"}
                    >
                      <option value="all">All changes</option>
                      <option value="upload">To Drive</option>
                      <option value="download">To computer</option>
                      <option value="conflict">Conflicts</option>
                    </select>
                  </div>
                  <div className="file-table">
                    <div className="table-head">
                      <input
                        type="checkbox"
                        aria-label="Select all non-conflicting changes"
                        checked={
                          allSelectable.length > 0 &&
                          allSelectable.every((a) => selected.has(a.id))
                        }
                        onChange={selectAll}
                        disabled={!!busy || !allSelectable.length}
                      />
                      <span>NAME</span>
                      <span>STATUS</span>
                      <span>SIZE</span>
                    </div>
                    {rows.length ? (
                      rows.map((path) => {
                        const at = actions.filter((a) => a.path === path);
                        const hasConflict = at.some((a) => conflict(a.kind));
                        const chosen = at.find((a) => selected.has(a.id));
                        const first = at[0];
                        const entry = plan?.local[path] ?? plan?.remote[path];
                        return (
                          <div
                            key={path}
                            className={`file-row-wrap ${preview === path ? "previewed" : ""}`}
                          >
                            <div className="file-row">
                              <input
                                type="checkbox"
                                aria-label={`Select ${path}`}
                                checked={!!chosen}
                                disabled={!!busy || !first || hasConflict}
                                onChange={() =>
                                  first &&
                                  setSelected(
                                    toggleSelection(selected, first, actions),
                                  )
                                }
                              />
                              <button
                                className="file-name"
                                onClick={() => setPreview(path)}
                              >
                                <FileIcon path={path} />
                                <span>
                                  <strong>{path.split("/").pop()}</strong>
                                  <small>
                                    {path.includes("/")
                                      ? path.slice(0, path.lastIndexOf("/"))
                                      : "Project root"}
                                  </small>
                                </span>
                              </button>
                              <span className={`status-pill ${statusOf(at)}`}>
                                {hasConflict ? (
                                  <>
                                    <SlidersHorizontal size={12} />
                                    Conflict
                                  </>
                                ) : first ? (
                                  <>
                                    {uploads(first.kind) ? (
                                      <ArrowUp size={12} />
                                    ) : (
                                      <ArrowDown size={12} />
                                    )}
                                    <span>
                                      {first.kind.startsWith("delete")
                                        ? "Remove"
                                        : uploads(first.kind)
                                          ? "Upload"
                                          : "Download"}
                                    </span>
                                  </>
                                ) : (
                                  <>
                                    <Check size={12} />
                                    Synced
                                  </>
                                )}
                              </span>
                              <span className="file-size">
                                {formatBytes(entry?.size ?? 0)}
                              </span>
                            </div>
                            {hasConflict && (
                              <div className="conflict-choice">
                                <span>Keep which copy?</span>
                                {at.map((a) => (
                                  <button
                                    key={a.id}
                                    disabled={!!busy}
                                    aria-pressed={selected.has(a.id)}
                                    className={
                                      selected.has(a.id) ? "chosen" : ""
                                    }
                                    onClick={() =>
                                      setSelected(
                                        toggleSelection(selected, a, actions),
                                      )
                                    }
                                  >
                                    {selected.has(a.id) && <Check size={12} />}
                                    <span>
                                      {uploads(a.kind)
                                        ? "Local"
                                        : "Google Drive"}
                                    </span>
                                  </button>
                                ))}
                              </div>
                            )}
                          </div>
                        );
                      })
                    ) : (
                      <div className="files-empty">
                        {busy === "Scanning folders" ? (
                          <>
                            <LoaderCircle size={28} className="spin" />
                            <strong>Getting the full picture</strong>
                            <p>
                              Comparing files on this computer and in Drive.
                            </p>
                          </>
                        ) : query ? (
                          <>
                            <Search size={27} />
                            <strong>No files match “{query}”</strong>
                            <p>Try another name or clear your search.</p>
                          </>
                        ) : plan ? (
                          <>
                            <CircleCheck size={32} />
                            <strong>
                              {tab === "changes"
                                ? "You’re all caught up"
                                : "This project is empty"}
                            </strong>
                            <p>
                              {tab === "changes"
                                ? "No changes in this view. Your next idea can take it from here."
                                : "Add CAD files to either connected folder and scan again."}
                            </p>
                          </>
                        ) : (
                          <>
                            <Boxes size={30} />
                            <strong>Ready for a fresh look</strong>
                            <p>
                              Scan both folders to review files and changes.
                            </p>
                          </>
                        )}
                      </div>
                    )}
                  </div>
                  <div className="table-footer">
                    <span>
                      {rows.length} {rows.length === 1 ? "file" : "files"}
                      {selected.size > 0 ? ` · ${selected.size} selected` : ""}
                    </span>
                    <span>
                      <ShieldCheck size={12} />
                      You choose what syncs
                    </span>
                  </div>
                </section>
                {preview && (
                  <Suspense
                    fallback={
                      <div className="preview-panel loading-preview">
                        Opening preview…
                      </div>
                    }
                  >
                    <CadPreview
                      path={preview}
                      sample={demo && /\.(step|stl)$/i.test(preview)}
                      load={loadPreview}
                      onClose={() => setPreview("")}
                    />
                  </Suspense>
                )}
              </div>
              <div className="sync-bar">
                <div>
                  <span className="sync-check">
                    <ShieldCheck size={20} />
                  </span>
                  <div>
                    <strong>
                      {busy ||
                        (selected.size
                          ? `${selected.size} ${selected.size === 1 ? "change" : "changes"} ready to sync`
                          : "A little review. A lot of peace of mind.")}
                    </strong>
                    <small>
                      {progress
                        ? `${progress.path} · ${formatBytes(progress.bytes)} / ${formatBytes(progress.size)} · ${progress.completed}/${progress.total}`
                        : "Select your changes. Replacements and removals get a final review."}
                    </small>
                  </div>
                </div>
                {busy ? (
                  <button
                    className="secondary"
                    onClick={() =>
                      void api.cancel().catch((e) => setError(String(e)))
                    }
                  >
                    Cancel
                  </button>
                ) : (
                  <button
                    className="primary"
                    disabled={!selected.size || !plan}
                    onClick={() => setModal("review")}
                  >
                    Review & sync
                    {selected.size > 0 && (
                      <span className="button-count">{selected.size}</span>
                    )}
                    <ArrowRight size={16} />
                  </button>
                )}
              </div>
              <div className="workspace-footnote">
                <span>
                  <LockKeyhole size={12} />
                  Your files stay yours. Previews are rendered on this device.
                </span>
                <span>Made for the way you build.</span>
              </div>
            </>
          )}
        </div>
      </main>
      {modal === "project" && (
        <ProjectDialog
          defaultFolder={data?.settings.defaultDriveFolder ?? ""}
          busy={busy}
          onClose={() => setModal(null)}
          onCreate={async (p) => {
            await run("Connecting project", async () => {
              await api.saveProject(p);
              setData((d) => (d ? { ...d, projects: [...d.projects, p] } : d));
              setProjectId(p.id);
              setScreen("workspace");
              setModal(null);
            });
          }}
        />
      )}
      {modal === "review" && (
        <ReviewDialog
          keepLocalBackups={data?.settings.keepLocalBackups ?? true}
          actions={selectedActions}
          onClose={() => setModal(null)}
          onConfirm={() => void doSync()}
        />
      )}
      {modal === "remove" && project && (
        <Dialog title="Remove this project?" onClose={() => setModal(null)}>
          <p className="dialog-description">
            Remove <strong>{project.name}</strong> from this workspace. Your
            local files and Google Drive folder are kept.
          </p>
          <div className="dialog-actions">
            <button className="secondary" onClick={() => setModal(null)}>
              Keep project
            </button>
            <button
              className="danger"
              onClick={() =>
                void run("Removing project", async () => {
                  await api.removeProject(project.id);
                  const projects = data!.projects.filter(
                    (p) => p.id !== project.id,
                  );
                  setData({ ...data!, projects });
                  setProjectId(projects[0]?.id ?? "");
                  setPlan(undefined);
                  setModal(null);
                })
              }
            >
              Remove project
            </button>
          </div>
        </Dialog>
      )}
      {modal === "disconnect" && (
        <Dialog
          title="Disconnect Google access?"
          onClose={() => setModal(null)}
        >
          <p className="dialog-description">
            This revokes Permafrost’s Google authorization, which can affect
            other devices using the same Google app. Your files remain in place.
            You can also sign out locally from the sidebar.
          </p>
          <div className="dialog-actions">
            <button className="secondary" onClick={() => setModal(null)}>
              Cancel
            </button>
            <button
              className="danger"
              onClick={() =>
                void run("Disconnecting Google", async () => {
                  await api.signOut(true);
                  setData((d) => (d ? { ...d, account: null } : d));
                  setModal(null);
                })
              }
            >
              Disconnect Google
            </button>
          </div>
        </Dialog>
      )}
    </div>
  );
}

function ProjectDialog({
  defaultFolder,
  busy,
  onClose,
  onCreate,
}: {
  defaultFolder: string;
  busy: string;
  onClose: () => void;
  onCreate: (p: Project) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [root, setRoot] = useState("");
  const [drive, setDrive] = useState(defaultFolder);
  const [readonly, setReadonly] = useState(false);
  const [error, setError] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const id = driveFolderId(drive);
    if (!name.trim() || !root || !id) {
      setError(
        "Enter a name, choose a local folder, and paste a valid Drive folder link.",
      );
      return;
    }
    void onCreate({
      id: crypto.randomUUID(),
      name: name.trim(),
      localRoot: root,
      driveFolderId: id,
      readOnly: readonly,
    });
  };
  return (
    <Dialog title="Connect a project" onClose={onClose}>
      <p className="dialog-description">
        One local folder. One Google Drive folder. A shared place for your next
        great build.
      </p>
      <form onSubmit={submit}>
        <label className="field">
          Project name
          <input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Field kit"
            maxLength={120}
            required
          />
        </label>
        <label className="field">
          Local folder
          <div className="input-with-button">
            <input
              value={root}
              readOnly
              placeholder="Choose a folder on this computer"
              required
            />
            <button
              type="button"
              className="secondary"
              onClick={() =>
                void api
                  .chooseDirectory()
                  .then((path) => {
                    if (path) setRoot(path);
                  })
                  .catch((e) => setError(String(e)))
              }
            >
              <FolderOpen size={16} />
              Browse
            </button>
          </div>
        </label>
        <label className="field">
          Google Drive folder link
          <input
            value={drive}
            onChange={(e) => setDrive(e.target.value)}
            placeholder="https://drive.google.com/drive/folders/…"
            required
          />
          <small>Use an existing folder you have permission to access.</small>
        </label>
        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={readonly}
            onChange={(e) => setReadonly(e.target.checked)}
          />
          <div>
            <strong>Make this project read-only</strong>
            <small>
              Download and review Drive changes. Never change files in Drive.
            </small>
          </div>
        </label>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="dialog-actions">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="primary" disabled={!!busy}>
            <Plus size={16} />
            Connect project
          </button>
        </div>
      </form>
    </Dialog>
  );
}
function ReviewDialog({
  actions,
  onClose,
  onConfirm,
  keepLocalBackups,
}: {
  actions: Action[];
  onClose: () => void;
  onConfirm: () => void;
  keepLocalBackups: boolean;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const changed = actions.filter((a) => destructive(a.kind));
  return (
    <Dialog title="One last look" wide onClose={onClose}>
      <p className="dialog-description">
        You’re about to sync {actions.length}{" "}
        {actions.length === 1 ? "change" : "changes"}.{" "}
        {keepLocalBackups ? (
          <>
            Local copies replaced or removed are saved in the project’s{" "}
            <code>.permafrost/backups</code> folder.
          </>
        ) : (
          <strong>
            Local backups are disabled. Local replacements and removals are
            permanent.
          </strong>
        )}{" "}
        Removed Drive files go to trash.
      </p>
      <div className="review-list">
        {actions.map((a) => (
          <div key={a.id}>
            <FileIcon path={a.path} />
            <span>
              <strong>{a.path}</strong>
              <small>{a.reason}</small>
            </span>
            <span
              className={`review-action ${destructive(a.kind) ? "destructive" : ""}`}
            >
              {actionLabel[a.kind]}
            </span>
          </div>
        ))}
      </div>
      {changed.length > 0 && (
        <label className="checkbox-field review-consent">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
          />
          <span>
            I reviewed the {changed.length}{" "}
            {changed.length === 1
              ? "replacement or removal"
              : "replacements or removals"}{" "}
            listed above and want to apply them.
          </span>
        </label>
      )}
      <div className="dialog-actions">
        <button className="secondary" onClick={onClose}>
          Back to changes
        </button>
        <button
          className="primary"
          disabled={changed.length > 0 && !confirmed}
          onClick={onConfirm}
        >
          Sync {actions.length} {actions.length === 1 ? "change" : "changes"}
          <ArrowRight size={16} />
        </button>
      </div>
    </Dialog>
  );
}

function SettingsPage({
  data,
  busy,
  onSave,
  onSignIn,
  onDisconnect,
  onExport,
  updateMessage,
  checkUpdates,
  installUpdate,
  onReorder,
}: {
  data: Bootstrap;
  busy: string;
  onSave: (s: Settings) => void;
  onSignIn: () => void;
  onDisconnect: () => void;
  onExport: () => void;
  updateMessage: string;
  checkUpdates: () => void;
  installUpdate?: () => void;
  onReorder: (ids: string[]) => void;
}) {
  const [settings, setSettings] = useState(data.settings);
  useEffect(() => setSettings(data.settings), [data.settings]);
  const reorder = (index: number, delta: number) => {
    const ids = data.projects.map((p) => p.id);
    [ids[index], ids[index + delta]] = [ids[index + delta], ids[index]];
    onReorder(ids);
  };
  return (
    <div className="settings-page">
      <div className="page-heading">
        <div>
          <div className="eyebrow">MAKE YOURSELF AT HOME</div>
          <h1>Settings</h1>
          <p>A few preferences for a smoother workflow.</p>
        </div>
        <span className="version-badge">v{data.version}</span>
      </div>
      <section className="settings-card">
        <div className="section-icon">
          <GoogleDrive size={22} />
        </div>
        <div className="settings-body">
          <h2>Google Drive</h2>
          <p>
            {data.account
              ? `Connected as ${data.account.email}`
              : "Sign in through your browser to connect your project folders."}
          </p>
          {!data.googleConfigured && (
            <p className="setup-note">
              This build needs the distributor’s Google Desktop OAuth
              configuration before sign-in is available.
            </p>
          )}
          {data.account ? (
            <button
              className="secondary"
              disabled={!!busy}
              onClick={onDisconnect}
            >
              Disconnect Google access
            </button>
          ) : (
            <button
              className="primary"
              disabled={!!busy || !data.googleConfigured}
              onClick={onSignIn}
            >
              Sign in with Google
              <ArrowRight size={14} />
            </button>
          )}
        </div>
      </section>
      <section className="settings-card">
        <div className="section-icon">
          <SlidersHorizontal size={22} />
        </div>
        <div className="settings-body">
          <h2>Your workflow</h2>
          <label className="field">
            Default project directory
            <div className="input-with-button">
              <input
                value={settings.workingDirectory}
                readOnly
                placeholder="Choose your default directory"
              />
              <button
                className="secondary"
                onClick={() =>
                  void api.chooseDirectory().then((path) => {
                    if (path)
                      setSettings({ ...settings, workingDirectory: path });
                  })
                }
              >
                Browse
              </button>
            </div>
          </label>
          <label className="field">
            Default Drive folder
            <input
              value={settings.defaultDriveFolder}
              onChange={(e) =>
                setSettings({ ...settings, defaultDriveFolder: e.target.value })
              }
              placeholder="Folder link or ID"
            />
          </label>
          <label className="checkbox-field">
            <input
              type="checkbox"
              checked={settings.keepRevisionsForever}
              onChange={(e) =>
                setSettings({
                  ...settings,
                  keepRevisionsForever: e.target.checked,
                })
              }
            />
            <div>
              <strong>Keep uploaded revisions forever in Drive</strong>
              <small>
                Uses Google Drive’s permanent revision retention. Drive’s
                retained-revision limits apply.
              </small>
            </div>
          </label>
          <label className="checkbox-field">
            <input
              type="checkbox"
              checked={settings.keepLocalBackups}
              onChange={(e) =>
                setSettings({ ...settings, keepLocalBackups: e.target.checked })
              }
            />
            <div>
              <strong>Keep local recovery copies</strong>
              <small>
                Save replaced and removed local files in .permafrost/backups.
                Disabling this makes those local changes permanent.
              </small>
            </div>
          </label>
          <label className="checkbox-field">
            <input
              type="checkbox"
              checked={settings.closeToTray}
              onChange={(e) =>
                setSettings({ ...settings, closeToTray: e.target.checked })
              }
            />
            <div>
              <strong>Keep Permafrost in the system tray</strong>
              <small>
                Closing the window keeps the app available. Quit from the tray
                menu.
              </small>
            </div>
          </label>
          <label className="checkbox-field">
            <input
              type="checkbox"
              checked={settings.checkUpdates}
              onChange={(e) =>
                setSettings({ ...settings, checkUpdates: e.target.checked })
              }
            />
            <div>
              <strong>Check for updates when Permafrost opens</strong>
              <small>You choose when to install a signed release.</small>
            </div>
          </label>
          <button
            className="primary"
            disabled={!!busy}
            onClick={() => onSave(settings)}
          >
            Save preferences
            <Check size={15} />
          </button>
        </div>
      </section>
      {data.projects.length > 1 && (
        <section className="settings-card">
          <div className="section-icon">
            <Folder size={22} />
          </div>
          <div className="settings-body">
            <h2>Project order</h2>
            <p>Keep your most active projects close.</p>
            <div className="project-order">
              {data.projects.map((p, i) => (
                <div key={p.id}>
                  <Folder size={16} />
                  <strong>{p.name}</strong>
                  <button
                    className="icon-button"
                    disabled={!!busy || i === 0}
                    aria-label={`Move ${p.name} up`}
                    onClick={() => reorder(i, -1)}
                  >
                    <ArrowUp size={15} />
                  </button>
                  <button
                    className="icon-button"
                    disabled={!!busy || i === data.projects.length - 1}
                    aria-label={`Move ${p.name} down`}
                    onClick={() => reorder(i, 1)}
                  >
                    <ArrowDown size={15} />
                  </button>
                </div>
              ))}
            </div>
          </div>
        </section>
      )}
      <section className="settings-card">
        <div className="section-icon">
          <Sparkles size={22} />
        </div>
        <div className="settings-body">
          <h2>A little better with every release</h2>
          <p>
            Permafrost {data.version}. Updates are verified before installation.
          </p>
          {updateMessage && <p role="status">{updateMessage}</p>}
          <button
            className="secondary"
            disabled={!!busy}
            onClick={checkUpdates}
          >
            <RefreshCw size={15} />
            Check for updates
          </button>
          {installUpdate && (
            <button
              className="primary"
              disabled={!!busy}
              onClick={installUpdate}
            >
              Install update & restart
            </button>
          )}
        </div>
      </section>
      <section className="settings-card">
        <div className="section-icon">
          <ShieldCheck size={22} />
        </div>
        <div className="settings-body">
          <h2>Local diagnostics</h2>
          <p>
            Export app version, configuration status, and transfer counts.
            Reports exclude credentials, account details, filenames, and file
            content. Nothing is uploaded.
          </p>
          <button className="secondary" disabled={!!busy} onClick={onExport}>
            <ArrowDown size={15} />
            Export diagnostics
          </button>
        </div>
      </section>
    </div>
  );
}
function ActivityPage({
  activity,
  projects,
  busy,
  onRefresh,
  onResolve,
}: {
  activity: Activity[];
  projects: Project[];
  busy: string;
  onRefresh: () => void;
  onResolve: (id: string) => void;
}) {
  const [review, setReview] = useState<Activity | null>(null);
  const [checked, setChecked] = useState(false);
  return (
    <div>
      <div className="page-heading">
        <div>
          <div className="eyebrow">EVERY CHANGE HAS A STORY</div>
          <h1>Activity</h1>
          <p>A clear record of what moved, and what needs a closer look.</p>
        </div>
        <button className="secondary" onClick={onRefresh} disabled={!!busy}>
          <RefreshCw size={16} />
          Refresh
        </button>
      </div>
      {activity.some((a) => a.state === "uncertain") && (
        <div className="notice warning">
          <TriangleAlert size={18} />
          <span>
            An interrupted transfer needs review. Inspect the local file, its
            backup, and its Drive copy before resolving.
          </span>
        </div>
      )}
      <section className="activity-list">
        {activity.length ? (
          activity.map((a) => (
            <div className="activity-row" key={a.id}>
              <span className={`activity-icon ${a.state}`}>
                {a.state === "completed" ? (
                  <Check size={18} />
                ) : a.state === "uncertain" ? (
                  <TriangleAlert size={18} />
                ) : (
                  <History size={18} />
                )}
              </span>
              <div>
                <strong>{a.path}</strong>
                <p>
                  {actionLabel[a.kind]} ·{" "}
                  {projects.find((p) => p.id === a.projectId)?.name ??
                    "Removed project"}
                </p>
                <small>{a.message}</small>
              </div>
              <div className="activity-state">
                <span className={`status-pill ${a.state}`}>{a.state}</span>
                <small>{new Date(a.createdAt * 1000).toLocaleString()}</small>
                {a.state === "uncertain" && (
                  <button
                    className="text-button"
                    disabled={!!busy}
                    onClick={() => {
                      setReview(a);
                      setChecked(false);
                    }}
                  >
                    Review recovery
                    <ArrowRight size={13} />
                  </button>
                )}
              </div>
            </div>
          ))
        ) : (
          <div className="empty-state">
            <History size={34} />
            <h2>A fresh start</h2>
            <p>Your completed and interrupted transfers will appear here.</p>
          </div>
        )}
      </section>
      {review && (
        <Dialog
          title="Review an interrupted transfer"
          onClose={() => setReview(null)}
        >
          <p className="dialog-description">
            Permafrost cannot prove whether <strong>{review.path}</strong>{" "}
            finished transferring. Review the actual file on both sides and
            check <code>.permafrost/backups</code> for a saved local copy.
            Resolving does not retry the transfer or declare either copy
            correct.
          </p>
          <label className="checkbox-field">
            <input
              type="checkbox"
              checked={checked}
              onChange={(e) => setChecked(e.target.checked)}
            />
            <span>
              I inspected both copies and any local backup. I’m ready to resolve
              this record and run a fresh scan.
            </span>
          </label>
          <div className="dialog-actions">
            <button className="secondary" onClick={() => setReview(null)}>
              Keep for later
            </button>
            <button
              className="primary"
              disabled={!checked}
              onClick={() => {
                onResolve(review.id);
                setReview(null);
              }}
            >
              Mark reviewed
              <Check size={14} />
            </button>
          </div>
        </Dialog>
      )}
    </div>
  );
}
