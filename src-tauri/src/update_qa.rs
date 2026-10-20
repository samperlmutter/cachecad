//! Compiled only for isolated two-version installer tests, never production.
use crate::{err, ApiResult};
use serde::Deserialize;
use std::{
    path::PathBuf,
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};
use tauri_plugin_updater::UpdaterExt;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Config {
    endpoint: String,
    target_version: String,
    receipt: PathBuf,
}

pub fn start(app: &tauri::AppHandle) -> ApiResult<bool> {
    let args: Vec<String> = std::env::args().collect();
    let Some(path) = args
        .windows(2)
        .find(|a| a[0] == "--update-qa")
        .map(|a| &a[1])
    else {
        return Ok(false);
    };
    // The QA build must still use the isolated profile and disable Drive/watchers.
    if !args.iter().any(|a| a == "--smoke-output") {
        return Err("Upgrade QA requires an isolated smoke profile.".into());
    }
    let config: Config = serde_json::from_slice(&std::fs::read(path).map_err(err)?).map_err(err)?;
    if app.package_info().version.to_string() == config.target_version {
        return Ok(false);
    }
    static STARTED: AtomicBool = AtomicBool::new(false);
    if STARTED.swap(true, Ordering::SeqCst) {
        return Ok(true);
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let result = upgrade(&app, &config).await;
        if let Err(error) = result {
            let _ = std::fs::write(
                &config.receipt,
                serde_json::json!({"error":error}).to_string(),
            );
            app.exit(3);
        }
    });
    Ok(true)
}

async fn upgrade(app: &tauri::AppHandle, config: &Config) -> ApiResult<()> {
    let endpoint = config.endpoint.parse::<tauri::Url>().map_err(err)?;
    if endpoint.scheme() != "http" || endpoint.host_str() != Some("127.0.0.1") {
        return Err("Upgrade QA only accepts a loopback HTTP test server.".into());
    }
    let update = app
        .updater_builder()
        .endpoints(vec![endpoint])
        .map_err(err)?
        .timeout(Duration::from_secs(45))
        .build()
        .map_err(err)?
        .check()
        .await
        .map_err(err)?
        .ok_or("The installed older version did not detect its signed upgrade.")?;
    if update.version != config.target_version {
        return Err("The upgrade did not match the expected target version.".into());
    }
    // The fixture serves a modified package first. Reject it before any install.
    match update.download(|_, _| {}, || {}).await {
        Err(tauri_plugin_updater::Error::Minisign(_)) => {}
        Err(error) => return Err(format!("Expected signature rejection, received {error}")),
        Ok(_) => return Err("The updater accepted a modified package.".into()),
    }
    let bytes = update.download(|_, _| {}, || {}).await.map_err(err)?;
    std::fs::write(
        &config.receipt,
        serde_json::json!({
            "fromVersion": app.package_info().version.to_string(),
            "toVersion": update.version,
            "tamperedPackageRejected": true,
            "validSignatureVerified": true,
            "downloadedBytes": bytes.len()
        })
        .to_string(),
    )
    .map_err(err)?;
    update.install(bytes).map_err(err)?;
    // NSIS exits the process and restarts with the same arguments itself.
    #[cfg(not(windows))]
    app.restart();
    #[cfg(windows)]
    Ok(())
}
