//! The device half of the Android update path: fetch the published manifest,
//! download the APK it names, and hand the verified file to the installer.
//!
//! A downloaded file is trusted only after its SHA-256 matches the manifest.
//! The Kotlin plugin then proves the archive carries this app's package name
//! and signing certificate before the system installer is asked to run, so a
//! manifest that points at a foreign APK ends at the download page instead.

use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use serde::Deserialize;
use sha2::{Digest, Sha256};
use tauri::plugin::{Builder as PluginBuilder, PluginHandle, TauriPlugin};
use tauri::{AppHandle, Manager, Runtime};

use crate::update::{self, Manifest};

const UPDATE_PLUGIN_CLASS: &str = "AvaUpdatePlugin";
const UPDATE_PLUGIN_NAME: &str = "avaupdate";
/// The rolling release's manifest asset. Fixed on purpose: the client must
/// never accept an update location chosen by a page or by stored settings.
const MANIFEST_URL: &str =
    "https://github.com/zhiyuan-zhang0206/Ava/releases/download/android-latest/android-latest.json";
/// The manifest is a few hundred bytes; a slow network is not a reason to keep
/// a page waiting for the check.
const MANIFEST_TIMEOUT: Duration = Duration::from_secs(20);
/// The APK is tens of megabytes. This bounds one whole download-and-hash step.
const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(600);
/// A release APK is far below this; the cap stops a corrupt manifest from
/// filling the device's cache.
const MAX_APK_BYTES: u64 = 512 * 1024 * 1024;
const CHUNK_BYTES: usize = 64 * 1024;

/// One install at a time: two concurrent runs would interleave writes to the
/// same cache file.
static INSTALL_IN_FLIGHT: AtomicBool = AtomicBool::new(false);

#[derive(Clone)]
struct UpdatePlugin<R: Runtime>(PluginHandle<R>);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum UpdateFailure {
    Busy,
    Bridge,
    Digest,
    Install,
    Manifest,
    Network,
    Storage,
    TooLarge,
}

impl UpdateFailure {
    /// Stable code for the page; the technical detail stays in the log.
    pub const fn code(self) -> &'static str {
        match self {
            Self::Busy => "busy",
            Self::Bridge => "bridge",
            Self::Digest => "digest",
            Self::Install => "install",
            Self::Manifest => "manifest",
            Self::Network => "network",
            Self::Storage => "storage",
            Self::TooLarge => "too-large",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InstallState {
    InstallerOpened,
    PermissionRequired,
    Current,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct VersionInfo {
    version_code: u64,
}

#[derive(Deserialize)]
struct InstallResult {
    status: String,
    #[serde(default)]
    reason: Option<String>,
}

/// Register the Kotlin installer bridge.
pub fn plugin<R: Runtime>() -> TauriPlugin<R> {
    PluginBuilder::new(UPDATE_PLUGIN_NAME)
        .setup(|app, api| {
            match api.register_android_plugin(crate::android::PLUGIN_PACKAGE, UPDATE_PLUGIN_CLASS) {
                Ok(handle) => {
                    app.manage(UpdatePlugin(handle));
                }
                // Without the bridge the app still runs and can point the user
                // at the download page; refusing to launch would be worse.
                Err(err) => log::error!("Android update plugin unavailable: {err}"),
            }
            Ok(())
        })
        .build()
}

/// The build number of the running APK, from the generated `BuildConfig`.
pub async fn version_code<R: Runtime>(app: &AppHandle<R>) -> Result<u64, UpdateFailure> {
    let plugin = app
        .try_state::<UpdatePlugin<R>>()
        .map(|plugin| plugin.0.clone())
        .ok_or(UpdateFailure::Bridge)?;
    // See the main-thread rule in android.rs: never call this synchronously.
    plugin
        .run_mobile_plugin_async::<VersionInfo>("info", serde_json::json!({}))
        .await
        .map(|info| info.version_code)
        .map_err(|err| {
            log::error!("could not read the Android build number: {err}");
            UpdateFailure::Bridge
        })
}

/// Return the manifest when it describes a build newer than `installed`.
pub async fn check(installed: u64) -> Result<Option<Manifest>, UpdateFailure> {
    let manifest = fetch().await?;
    Ok(update::is_newer(&manifest, installed).then_some(manifest))
}

pub async fn install<R: Runtime>(app: &AppHandle<R>) -> Result<InstallState, UpdateFailure> {
    if INSTALL_IN_FLIGHT
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        return Err(UpdateFailure::Busy);
    }
    let outcome = install_verified(app).await;
    INSTALL_IN_FLIGHT.store(false, Ordering::Release);
    outcome
}

async fn install_verified<R: Runtime>(app: &AppHandle<R>) -> Result<InstallState, UpdateFailure> {
    let installed = version_code(app).await?;
    // Re-read the manifest instead of trusting an earlier check: the file that
    // reaches the installer must be the one this run verifies end to end.
    let manifest = fetch().await?;
    if !update::is_newer(&manifest, installed) {
        return Ok(InstallState::Current);
    }
    let cache_dir = app
        .path()
        .app_cache_dir()
        .map_err(|_| UpdateFailure::Storage)?;
    let path = tauri::async_runtime::spawn_blocking(move || download(&cache_dir, &manifest))
        .await
        .map_err(|_| UpdateFailure::Network)??;
    let plugin = app
        .try_state::<UpdatePlugin<R>>()
        .map(|plugin| plugin.0.clone())
        .ok_or(UpdateFailure::Bridge)?;
    // See the main-thread rule in android.rs: never call this synchronously.
    let result = plugin
        .run_mobile_plugin_async::<InstallResult>(
            "install",
            serde_json::json!({ "path": path.to_string_lossy() }),
        )
        .await
        .map_err(|err| {
            log::error!("update installer call failed: {err}");
            UpdateFailure::Bridge
        })?;
    match result.status.as_str() {
        "installer-opened" => Ok(InstallState::InstallerOpened),
        "permission-required" => Ok(InstallState::PermissionRequired),
        other => {
            log::warn!(
                "the system installer refused the update: {other} ({:?})",
                result.reason
            );
            Err(UpdateFailure::Install)
        }
    }
}

async fn fetch() -> Result<Manifest, UpdateFailure> {
    let manifest = tauri::async_runtime::spawn_blocking(fetch_manifest)
        .await
        .map_err(|_| UpdateFailure::Network)?;
    manifest
}

fn fetch_manifest() -> Result<Manifest, UpdateFailure> {
    let response = client(MANIFEST_TIMEOUT)?
        .get(MANIFEST_URL)
        .header(reqwest::header::ACCEPT, "application/json")
        .send()
        .map_err(|_| UpdateFailure::Network)?;
    if !response.status().is_success() {
        log::warn!(
            "update manifest answered HTTP {}",
            response.status().as_u16()
        );
        return Err(UpdateFailure::Network);
    }
    let body = response.text().map_err(|_| UpdateFailure::Network)?;
    update::parse_manifest(&body).map_err(|err| {
        log::warn!("update manifest rejected: {err}");
        UpdateFailure::Manifest
    })
}

fn client(timeout: Duration) -> Result<reqwest::blocking::Client, UpdateFailure> {
    reqwest::blocking::Client::builder()
        .timeout(timeout)
        .redirect(reqwest::redirect::Policy::limited(5))
        .build()
        .map_err(|_| UpdateFailure::Network)
}

fn download(cache_dir: &Path, manifest: &Manifest) -> Result<PathBuf, UpdateFailure> {
    let directory = cache_dir.join("updates");
    fs::create_dir_all(&directory).map_err(|_| UpdateFailure::Storage)?;
    // A previous attempt may have left a partial or superseded file; the
    // installer only ever reads the path returned below.
    for entry in fs::read_dir(&directory).map_err(|_| UpdateFailure::Storage)? {
        let path = entry.map_err(|_| UpdateFailure::Storage)?.path();
        if path.is_file() {
            let _ = fs::remove_file(path);
        }
    }
    let destination = directory.join(format!("ava-app-{}.apk", manifest.version_code));
    let mut response = client(DOWNLOAD_TIMEOUT)?
        .get(&manifest.apk_url)
        .send()
        .map_err(|_| UpdateFailure::Network)?;
    if !response.status().is_success() {
        log::warn!("update APK answered HTTP {}", response.status().as_u16());
        return Err(UpdateFailure::Network);
    }
    if response
        .content_length()
        .is_some_and(|length| length > MAX_APK_BYTES)
    {
        return Err(UpdateFailure::TooLarge);
    }
    let mut file = File::create(&destination).map_err(|_| UpdateFailure::Storage)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; CHUNK_BYTES];
    let mut written = 0_u64;
    loop {
        let read = response
            .read(&mut buffer)
            .map_err(|_| UpdateFailure::Network)?;
        if read == 0 {
            break;
        }
        written += read as u64;
        if written > MAX_APK_BYTES {
            drop(file);
            let _ = fs::remove_file(&destination);
            return Err(UpdateFailure::TooLarge);
        }
        hasher.update(&buffer[..read]);
        file.write_all(&buffer[..read])
            .map_err(|_| UpdateFailure::Storage)?;
    }
    drop(file);
    let actual = update::hex(hasher.finalize().as_slice());
    if !update::digest_matches(&manifest.sha256, &actual) {
        let _ = fs::remove_file(&destination);
        log::warn!(
            "update APK digest mismatch for build {}",
            manifest.version_code
        );
        return Err(UpdateFailure::Digest);
    }
    Ok(destination)
}
