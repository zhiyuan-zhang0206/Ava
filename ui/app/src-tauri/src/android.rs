//! Android specifics: notification permission and the foreground service.
//!
//! Background residency on Android is not a window property — an app whose
//! process is reaped loses its webview, and with it the SSE connection the
//! notification bridge rides on. A foreground service (with the persistent
//! notification Android requires in exchange) is the supported way to keep the
//! process alive, so the shell starts one while the setting is on.
//!
//! The service itself is Kotlin (`ui/app/android/`), reached through a tiny
//! Tauri Android plugin. Rust only turns it on and off.

use tauri::plugin::{Builder as PluginBuilder, PluginHandle, TauriPlugin};
use tauri::{AppHandle, Manager, Runtime};

/// Java package the overlay installs the plugin class into. Must match
/// `identifier` in `tauri.conf.json`, which is what the Android project's
/// package name is derived from.
const PLUGIN_PACKAGE: &str = "com.ava.shell";
const BACKGROUND_PLUGIN_CLASS: &str = "AvaBackgroundPlugin";
const BACKGROUND_PLUGIN_NAME: &str = "avabackground";
const SECRET_PLUGIN_CLASS: &str = "AvaSecretPlugin";
const SECRET_PLUGIN_NAME: &str = "avasecret";

/// Managed handle to the Kotlin side. Absent when the plugin failed to load,
/// which must degrade to "no background residency", never to a failed launch.
struct BackgroundPlugin<R: Runtime>(PluginHandle<R>);

/// Managed handle to the Keystore plugin. A missing handle is never treated as
/// an empty secret when saving: storing a credential must fail closed.
struct SecretPlugin<R: Runtime>(PluginHandle<R>);

#[derive(serde::Deserialize)]
struct StoredSecret {
    secret: Option<String>,
}

/// Register the Kotlin foreground-service plugin.
pub fn background_plugin<R: Runtime>() -> TauriPlugin<R> {
    PluginBuilder::new(BACKGROUND_PLUGIN_NAME)
        .setup(|app, api| {
            match api.register_android_plugin(PLUGIN_PACKAGE, BACKGROUND_PLUGIN_CLASS) {
                Ok(handle) => {
                    app.manage(BackgroundPlugin(handle));
                }
                // A missing class means the Android overlay was not applied to
                // this build. Losing background residency is bad; refusing to
                // start is worse.
                Err(err) => log::error!("foreground-service plugin unavailable: {err}"),
            }
            Ok(())
        })
        .build()
}

/// Register the Kotlin Android-Keystore plugin.
pub fn secret_plugin<R: Runtime>() -> TauriPlugin<R> {
    PluginBuilder::new(SECRET_PLUGIN_NAME)
        .setup(|app, api| {
            match api.register_android_plugin(PLUGIN_PACKAGE, SECRET_PLUGIN_CLASS) {
                Ok(handle) => {
                    app.manage(SecretPlugin(handle));
                }
                Err(err) => log::error!("Keystore secret plugin unavailable: {err}"),
            }
            Ok(())
        })
        .build()
}

/// Start or stop the foreground service.
pub fn set_background_service<R: Runtime>(app: &AppHandle<R>, enabled: bool) {
    let Some(plugin) = app.try_state::<BackgroundPlugin<R>>() else {
        return;
    };
    let command = if enabled {
        "startService"
    } else {
        "stopService"
    };
    if let Err(err) = plugin
        .0
        .run_mobile_plugin::<serde_json::Value>(command, serde_json::json!({}))
    {
        log::error!("foreground service {command} failed: {err}");
    }
}

/// Store the cluster secret after a successful native login only.
pub fn save_secret<R: Runtime>(app: &AppHandle<R>, secret: &str) -> Result<(), String> {
    let plugin = app
        .try_state::<SecretPlugin<R>>()
        .ok_or_else(|| "secure cluster-secret storage is unavailable".to_string())?;
    plugin
        .0
        .run_mobile_plugin::<serde_json::Value>("save", serde_json::json!({ "secret": secret }))
        .map(|_| ())
        .map_err(|_| "could not save the cluster secret securely".to_string())
}

/// Return the decrypted stored secret, if Keystore has one and can read it.
/// A startup failure simply falls back to the console's regular login page.
pub fn stored_secret<R: Runtime>(app: &AppHandle<R>) -> Option<String> {
    let plugin = app.try_state::<SecretPlugin<R>>()?;
    plugin
        .0
        .run_mobile_plugin::<StoredSecret>("get", serde_json::json!({}))
        .ok()?
        .secret
}

/// Clear an explicitly removed Android secret without affecting settings.json.
pub fn clear_secret<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    let plugin = app
        .try_state::<SecretPlugin<R>>()
        .ok_or_else(|| "secure cluster-secret storage is unavailable".to_string())?;
    plugin
        .0
        .run_mobile_plugin::<serde_json::Value>("clear", serde_json::json!({}))
        .map(|_| ())
        .map_err(|_| "could not clear the cluster secret".to_string())
}

/// Ask for the notification permission after the user enables notifications.
pub fn request_notification_permission<R: Runtime>(app: &AppHandle<R>) {
    use tauri_plugin_notification::NotificationExt;

    let notifier = app.notification();
    match notifier.permission_state() {
        Ok(tauri_plugin_notification::PermissionState::Granted) => {}
        // Android 13+ requires an explicit grant; a denial only costs
        // notifications, so the result is logged and not acted on.
        _ => {
            if let Err(err) = notifier.request_permission() {
                log::warn!("notification permission request failed: {err}");
            }
        }
    }
}

/// Startup wiring: restore the persisted notification permission and service
/// choices. Fresh installs default both off, so onboarding happens first.
pub fn setup<R: Runtime>(app: &AppHandle<R>) {
    let settings = app.state::<crate::state::ShellState>().settings();
    if settings.notifications {
        request_notification_permission(app);
    }

    if settings.background_service {
        set_background_service(app, true);
    }
}
