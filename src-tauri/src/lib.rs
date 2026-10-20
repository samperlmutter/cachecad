use permafrost_core::{
    auth::{Account, Auth, OAuthConfig},
    drive::Drive,
    model::*,
    planner,
    store::Store,
    transfer, Error,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    io::Read,
    path::PathBuf,
    sync::{Arc, Mutex},
};
use tauri::{Emitter, Manager, State};
use tokio::sync::{Mutex as AsyncMutex, OwnedSemaphorePermit, Semaphore};
use tokio_util::sync::CancellationToken;

type ApiResult<T> = std::result::Result<T, String>;
fn err(error: impl std::fmt::Display) -> String {
    error.to_string()
}
fn lock<T>(mutex: &Mutex<T>) -> ApiResult<std::sync::MutexGuard<'_, T>> {
    mutex
        .lock()
        .map_err(|_| "A background operation needs an application restart.".into())
}
struct Operation {
    _permit: OwnedSemaphorePermit,
    cancel: Arc<Mutex<Option<CancellationToken>>>,
}
impl Drop for Operation {
    fn drop(&mut self) {
        if let Ok(mut cancel) = self.cancel.lock() {
            *cancel = None;
        }
    }
}
struct Backend {
    store: Arc<Mutex<Store>>,
    auth: AsyncMutex<Auth>,
    restored: AsyncMutex<bool>,
    plans: Mutex<HashMap<String, Plan>>,
    gate: Arc<Semaphore>,
    cancel: Arc<Mutex<Option<CancellationToken>>>,
    profile: PathBuf,
    smoke_output: Option<PathBuf>,
    _temporary_profile: Option<tempfile::TempDir>,
    updater_configured: bool,
}
impl Backend {
    fn begin(&self) -> ApiResult<(Operation, CancellationToken)> {
        let permit = self.gate.clone().try_acquire_owned().map_err(|_| {
            "Another operation is still running. Wait for it to finish or cancel it.".to_string()
        })?;
        let token = CancellationToken::new();
        *lock(&self.cancel)? = Some(token.clone());
        Ok((
            Operation {
                _permit: permit,
                cancel: self.cancel.clone(),
            },
            token,
        ))
    }
    async fn drive(&self) -> ApiResult<Drive> {
        Drive::new(self.auth.lock().await.access_token().await.map_err(err)?).map_err(err)
    }
}
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Bootstrap {
    projects: Vec<Project>,
    settings: Settings,
    account: Option<Account>,
    google_configured: bool,
    version: String,
    activity: Vec<Activity>,
    account_error: Option<String>,
    updater_configured: bool,
}
#[tauri::command]
async fn bootstrap(state: State<'_, Backend>, app: tauri::AppHandle) -> ApiResult<Bootstrap> {
    let mut auth = state.auth.lock().await;
    let mut restored = state.restored.lock().await;
    let mut account_error = None;
    if !*restored && state.smoke_output.is_none() {
        if let Err(e) = auth.restore() {
            account_error = Some(e.to_string());
        }
        *restored = true;
    }
    let store = lock(&state.store)?;
    Ok(Bootstrap {
        projects: store.projects().map_err(err)?,
        settings: store.settings().map_err(err)?,
        account: auth.account(),
        google_configured: auth.configured(),
        version: app.package_info().version.to_string(),
        activity: store.activity().map_err(err)?,
        account_error,
        updater_configured: state.updater_configured,
    })
}
#[tauri::command]
async fn sign_in(state: State<'_, Backend>) -> ApiResult<Account> {
    let (_operation, cancel) = state.begin()?;
    state.auth.lock().await.sign_in(&cancel).await.map_err(err)
}
#[tauri::command]
async fn sign_out(state: State<'_, Backend>, revoke: bool) -> ApiResult<()> {
    let (_operation, _) = state.begin()?;
    lock(&state.plans)?.clear();
    let mut auth = state.auth.lock().await;
    if revoke {
        auth.disconnect().await.map_err(err)
    } else {
        auth.sign_out().map_err(err)
    }
}
#[tauri::command]
fn save_project(state: State<'_, Backend>, mut project: Project) -> ApiResult<()> {
    let (_operation, _) = state.begin()?;
    project.local_root = project.local_root.canonicalize().map_err(err)?;
    lock(&state.store)?.save_project(&project).map_err(err)?;
    lock(&state.plans)?.clear();
    Ok(())
}
#[tauri::command]
fn remove_project(state: State<'_, Backend>, project_id: String) -> ApiResult<()> {
    let (_operation, _) = state.begin()?;
    lock(&state.store)?
        .remove_project(&project_id)
        .map_err(err)?;
    lock(&state.plans)?.clear();
    Ok(())
}
#[tauri::command]
fn reorder_projects(state: State<'_, Backend>, ids: Vec<String>) -> ApiResult<()> {
    let (_operation, _) = state.begin()?;
    lock(&state.store)?.reorder(&ids).map_err(err)
}
#[tauri::command]
fn save_settings(state: State<'_, Backend>, settings: Settings) -> ApiResult<()> {
    let (_operation, _) = state.begin()?;
    lock(&state.store)?.save_settings(&settings).map_err(err)
}
#[tauri::command]
async fn scan_project(state: State<'_, Backend>, project_id: String) -> ApiResult<Plan> {
    let (_operation, cancel) = state.begin()?;
    lock(&state.plans)?.clear();
    let project = lock(&state.store)?.project(&project_id).map_err(err)?;
    let baseline = lock(&state.store)?.baseline(&project_id).map_err(err)?;
    let drive = state.drive().await?;
    let root = project.local_root.clone();
    let local_cancel = cancel.clone();
    let local = tauri::async_runtime::spawn_blocking(move || {
        permafrost_core::fs::scan(&root, &local_cancel)
    });
    let (local, remote) = tokio::join!(local, drive.scan(&project.drive_folder_id, &cancel));
    let local = local.map_err(err)?.map_err(err)?;
    let (remote, folders) = remote.map_err(err)?;
    if cancel.is_cancelled() {
        return Err(Error::Cancelled.to_string());
    }
    lock(&state.store)?
        .record_equal(&project.id, &local, &remote)
        .map_err(err)?;
    let plan = planner::plan(&project, local, remote, &baseline, folders);
    lock(&state.plans)?.insert(project_id, plan.clone());
    Ok(plan)
}
#[tauri::command]
async fn execute_sync(
    state: State<'_, Backend>,
    app: tauri::AppHandle,
    project_id: String,
    plan_id: String,
    action_ids: Vec<String>,
    consent: bool,
) -> ApiResult<transfer::Report> {
    let (_operation, cancel) = state.begin()?;
    let project = lock(&state.store)?.project(&project_id).map_err(err)?;
    let plan = lock(&state.plans)?
        .remove(&project_id)
        .filter(|p| p.id == plan_id)
        .ok_or_else(|| Error::Stale.to_string())?;
    let drive = state.drive().await?;
    transfer::execute(
        &state.store,
        &drive,
        &project,
        &plan,
        transfer::Selection {
            ids: &action_ids,
            consent,
        },
        &cancel,
        &|progress| {
            let _ = app.emit("sync-progress", progress);
        },
    )
    .await
    .map_err(err)
}
#[tauri::command]
fn cancel_operation(state: State<'_, Backend>) -> ApiResult<()> {
    if let Some(cancel) = lock(&state.cancel)?.as_ref() {
        cancel.cancel();
    }
    Ok(())
}
#[tauri::command]
fn activity(state: State<'_, Backend>) -> ApiResult<Vec<Activity>> {
    lock(&state.store)?.activity().map_err(err)
}
#[tauri::command]
fn resolve_operation(state: State<'_, Backend>, operation_id: String) -> ApiResult<()> {
    let (_operation, _) = state.begin()?;
    lock(&state.store)?.resolve(&operation_id).map_err(err)?;
    lock(&state.plans)?.clear();
    Ok(())
}
#[tauri::command]
fn reveal_project(state: State<'_, Backend>, project_id: String) -> ApiResult<()> {
    let p = lock(&state.store)?.project(&project_id).map_err(err)?;
    open::that_detached(p.local_root).map_err(err)
}
#[tauri::command]
fn open_drive(state: State<'_, Backend>, project_id: String) -> ApiResult<()> {
    let p = lock(&state.store)?.project(&project_id).map_err(err)?;
    open::that_detached(format!(
        "https://drive.google.com/drive/folders/{}",
        p.drive_folder_id
    ))
    .map_err(err)
}
#[tauri::command]
async fn read_preview(
    state: State<'_, Backend>,
    project_id: String,
    path: String,
) -> ApiResult<tauri::ipc::Response> {
    let project = lock(&state.store)?.project(&project_id).map_err(err)?;
    tauri::async_runtime::spawn_blocking(move || {
        let ext = std::path::Path::new(&path)
            .extension()
            .and_then(|e| e.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        if ![
            "stl", "step", "stp", "iges", "igs", "brep", "brp", "obj", "glb",
        ]
        .contains(&ext.as_str())
        {
            return Err("Preview a STEP, IGES, BREP, STL, OBJ, or GLB export of this file.".into());
        }
        let target = permafrost_core::fs::safe_path(&project.local_root, &path).map_err(err)?;
        let file = std::fs::File::open(target).map_err(|_| {
            "This file is not on your computer yet. Download it before previewing.".to_string()
        })?;
        if !file.metadata().map_err(err)?.is_file() {
            return Err("Choose a model file to preview.".into());
        }
        let mut bytes = Vec::new();
        file.take(64 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)
            .map_err(err)?;
        if bytes.len() > 64 * 1024 * 1024 {
            return Err(
                "This file exceeds the 64 MB preview limit. Export a lighter model.".into(),
            );
        }
        Ok(tauri::ipc::Response::new(bytes))
    })
    .await
    .map_err(err)?
}
#[tauri::command]
fn export_diagnostics(state: State<'_, Backend>, app: tauri::AppHandle) -> ApiResult<String> {
    let store = lock(&state.store)?;
    let activity = store.activity().map_err(err)?;
    let report = serde_json::json!({"schema":1,"version":app.package_info().version.to_string(),"platform":std::env::consts::OS,"arch":std::env::consts::ARCH,"projectCount":store.projects().map_err(err)?.len(),"transferCounts":{"completed":activity.iter().filter(|a|a.state=="completed").count(),"failed":activity.iter().filter(|a|a.state=="failed").count(),"uncertain":activity.iter().filter(|a|a.state=="uncertain").count()},"updaterConfigured":state.updater_configured});
    let folder = state.profile.join("diagnostics");
    std::fs::create_dir_all(&folder).map_err(err)?;
    let path = folder.join(format!("permafrost-{}.json", now()));
    std::fs::write(&path, serde_json::to_vec_pretty(&report).map_err(err)?).map_err(err)?;
    Ok(path.display().to_string())
}
#[derive(Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct UiEvidence {
    workspace_visible: bool,
    model_rendered: bool,
    canvas_width: u32,
    canvas_height: u32,
    title: String,
}
#[tauri::command]
fn native_ready(
    state: State<'_, Backend>,
    app: tauri::AppHandle,
    evidence: UiEvidence,
) -> ApiResult<()> {
    let Some(output) = &state.smoke_output else {
        return Ok(());
    };
    if !evidence.workspace_visible
        || !evidence.model_rendered
        || evidence.canvas_width < 100
        || evidence.canvas_height < 100
        || evidence.title != "Permafrost"
    {
        return Err(
            "The installed WebView did not render the expected workspace and 3D model.".into(),
        );
    }
    let report = serde_json::json!({"schema":1,"version":app.package_info().version.to_string(),"platform":std::env::consts::OS,"arch":std::env::consts::ARCH,"installedExecutable":std::env::current_exe().map_err(err)?.display().to_string(),"isolatedProfile":true,"networkServicesDisabled":true,"ui":evidence});
    std::fs::write(output, serde_json::to_vec_pretty(&report).map_err(err)?).map_err(err)?;
    let handle = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(500));
        handle.exit(0);
    });
    Ok(())
}

pub fn run() {
    let args: Vec<String> = std::env::args().collect();
    let smoke_output = args
        .windows(2)
        .find(|a| a[0] == "--smoke-output")
        .map(|a| PathBuf::from(&a[1]));
    let smoke = smoke_output.is_some();
    let updater_configured = !smoke;
    let mut builder = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }
        }));
    if updater_configured {
        builder = builder.plugin(tauri_plugin_updater::Builder::new().build());
    }
    builder
        .setup(move |app| {
            let temporary = if smoke {
                Some(tempfile::tempdir()?)
            } else {
                None
            };
            let profile = temporary
                .as_ref()
                .map(|t| t.path().to_path_buf())
                .unwrap_or(app.path().app_data_dir()?);
            std::fs::create_dir_all(&profile)?;
            let backend = Backend {
                store: Arc::new(Mutex::new(Store::open(&profile.join("permafrost.sqlite"))?)),
                auth: AsyncMutex::new(Auth::new(if smoke {
                    None
                } else {
                    OAuthConfig::bundled()
                })?),
                restored: AsyncMutex::new(false),
                plans: Mutex::new(HashMap::new()),
                gate: Arc::new(Semaphore::new(1)),
                cancel: Arc::new(Mutex::new(None)),
                profile,
                smoke_output,
                _temporary_profile: temporary,
                updater_configured,
            };
            app.manage(backend);
            let show =
                tauri::menu::MenuItem::with_id(app, "show", "Open Permafrost", true, None::<&str>)?;
            let quit =
                tauri::menu::MenuItem::with_id(app, "quit", "Quit Permafrost", true, None::<&str>)?;
            let menu = tauri::menu::Menu::with_items(app, &[&show, &quit])?;
            let mut tray = tauri::tray::TrayIconBuilder::new()
                .menu(&menu)
                .tooltip("Permafrost · your work, connected")
                .on_menu_event(|app, event| {
                    if event.id == "quit" {
                        let state = app.state::<Backend>();
                        if state.gate.available_permits() > 0 {
                            app.exit(0);
                        } else {
                            if let Ok(cancel) = state.cancel.lock() {
                                if let Some(token) = cancel.as_ref() {
                                    token.cancel();
                                }
                            }
                            if let Some(w) = app.get_webview_window("main") {
                                let _ = w.show();
                                let _ = w.set_focus();
                            }
                            let _ = app.emit("quit-deferred", ());
                        }
                    } else if let Some(w) = app.get_webview_window("main") {
                        let _ = w.show();
                        let _ = w.set_focus();
                    }
                });
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(app)?;
            if smoke {
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    std::thread::sleep(std::time::Duration::from_secs(60));
                    handle.exit(2);
                });
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let state = window.state::<Backend>();
                let active = state.gate.available_permits() == 0;
                let tray = state
                    .store
                    .lock()
                    .ok()
                    .and_then(|s| s.settings().ok())
                    .is_some_and(|s| s.close_to_tray);
                if active || tray {
                    api.prevent_close();
                    if active {
                        if let Ok(cancel) = state.cancel.lock() {
                            if let Some(token) = cancel.as_ref() {
                                token.cancel();
                            }
                        }
                        let _ = window.emit("quit-deferred", ());
                    } else {
                        let _ = window.hide();
                    }
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            bootstrap,
            sign_in,
            sign_out,
            save_project,
            remove_project,
            reorder_projects,
            save_settings,
            scan_project,
            execute_sync,
            cancel_operation,
            activity,
            resolve_operation,
            reveal_project,
            open_drive,
            read_preview,
            export_diagnostics,
            native_ready
        ])
        .run(tauri::generate_context!())
        .expect("Permafrost could not start");
}
