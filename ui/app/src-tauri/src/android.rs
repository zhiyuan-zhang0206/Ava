//! Android specifics: native plugins, notification permission, and the
//! foreground service.
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
const CLICK_PLUGIN_CLASS: &str = "AvaClickPlugin";
const CLICK_PLUGIN_NAME: &str = "avaclick";

/// `run_mobile_plugin` must never be called on the Android main thread: the
/// JNI round-trip is serviced by the main looper, so a main-thread call
/// deadlocks waiting for the response it prevents. All command wrappers below
/// use the async API, and notification's synchronous public API runs on the
/// blocking executor for the same reason.

/// Managed handle to the Kotlin side. Absent when the plugin failed to load,
/// which must degrade to "no background residency", never to a failed launch.
#[derive(Clone)]
struct BackgroundPlugin<R: Runtime>(PluginHandle<R>);

/// Managed handle to the Keystore plugin. A missing handle is never treated as
/// an empty secret when saving: storing a credential must fail closed.
#[derive(Clone)]
struct SecretPlugin<R: Runtime>(PluginHandle<R>);

/// Managed handle to the notification-click mailbox. A missing overlay means
/// the shell continues without notification deep-link capture.
#[derive(Clone)]
struct ClickPlugin<R: Runtime>(PluginHandle<R>);

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

/// Register the Kotlin notification-click plugin.
pub fn click_plugin<R: Runtime>() -> TauriPlugin<R> {
    PluginBuilder::new(CLICK_PLUGIN_NAME)
        .setup(|app, api| {
            match api.register_android_plugin(PLUGIN_PACKAGE, CLICK_PLUGIN_CLASS) {
                Ok(handle) => {
                    app.manage(ClickPlugin(handle));
                }
                // A missing class means the Android overlay was not applied.
                // The shell still starts; it simply cannot capture click taps.
                Err(err) => log::error!("notification-click plugin unavailable: {err}"),
            }
            Ok(())
        })
        .build()
}

/// Consume the pending notification click, if the Android overlay is present.
pub async fn take_pending_click<R: Runtime>(app: &AppHandle<R>) -> Option<bool> {
    let Some(plugin) = app
        .try_state::<ClickPlugin<R>>()
        .map(|plugin| plugin.0.clone())
    else {
        return None;
    };
    // See the Android main-thread rule above: never use run_mobile_plugin here.
    match plugin
        .run_mobile_plugin_async::<serde_json::Value>("takePendingClick", serde_json::json!({}))
        .await
    {
        Ok(result) => result.get("pending").and_then(serde_json::Value::as_bool),
        Err(err) => {
            log::error!("could not consume notification click: {err}");
            None
        }
    }
}

/// Start or stop the foreground service without blocking the Android looper.
pub async fn set_background_service_async<R: Runtime>(app: &AppHandle<R>, enabled: bool) {
    let Some(plugin) = app
        .try_state::<BackgroundPlugin<R>>()
        .map(|plugin| plugin.0.clone())
    else {
        return;
    };
    let command = if enabled {
        "startService"
    } else {
        "stopService"
    };
    // See the Android main-thread rule above: never use run_mobile_plugin here.
    if let Err(err) = plugin
        .run_mobile_plugin_async::<serde_json::Value>(command, serde_json::json!({}))
        .await
    {
        log::error!("foreground service {command} failed: {err}");
    }
}

/// Store the cluster secret after a successful native login only.
pub async fn save_secret_async<R: Runtime>(app: &AppHandle<R>, secret: &str) -> Result<(), String> {
    let plugin = app
        .try_state::<SecretPlugin<R>>()
        .map(|plugin| plugin.0.clone())
        .ok_or_else(|| "secure cluster-secret storage is unavailable".to_string())?;
    // See the Android main-thread rule above: never use run_mobile_plugin here.
    plugin
        .run_mobile_plugin_async::<serde_json::Value>(
            "save",
            serde_json::json!({ "secret": secret }),
        )
        .await
        .map(|_| ())
        .map_err(|_| "could not save the cluster secret securely".to_string())
}

/// Load and cache the Keystore secret without blocking the Android main looper.
///
/// The first caller may observe no cached value while this request is in
/// flight; a read failure is equivalent to no startup credential.
pub async fn load_stored_secret<R: Runtime>(app: &AppHandle<R>) -> Option<String> {
    let state = app.state::<crate::state::ShellState>();
    if state.android_secret_loaded() {
        return state.android_secret();
    }

    let Some(plugin) = app
        .try_state::<SecretPlugin<R>>()
        .map(|plugin| plugin.0.clone())
    else {
        return state.cache_android_secret_if_unloaded(None);
    };
    // The Android main-thread rule above requires this async JNI path.
    let secret = plugin
        .run_mobile_plugin_async::<StoredSecret>("get", serde_json::json!({}))
        .await
        .ok()
        .and_then(|stored| stored.secret);
    state.cache_android_secret_if_unloaded(secret)
}

/// Clear an explicitly removed Android secret without affecting settings.json.
pub async fn clear_secret_async<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    let plugin = app
        .try_state::<SecretPlugin<R>>()
        .map(|plugin| plugin.0.clone())
        .ok_or_else(|| "secure cluster-secret storage is unavailable".to_string())?;
    // See the Android main-thread rule above: never use run_mobile_plugin here.
    plugin
        .run_mobile_plugin_async::<serde_json::Value>("clear", serde_json::json!({}))
        .await
        .map(|_| ())
        .map_err(|_| "could not clear the cluster secret".to_string())
}

/// Ask for Android notification permission without blocking the main looper.
pub async fn request_notification_permission_async<R: Runtime>(app: AppHandle<R>) {
    let result = tauri::async_runtime::spawn_blocking(move || {
        use tauri_plugin_notification::NotificationExt;

        let notifier = app.notification();
        match notifier.permission_state() {
            Ok(tauri_plugin_notification::PermissionState::Granted) => Ok(()),
            // Notification's public API is synchronous and calls
            // run_mobile_plugin internally, so this must stay off the main thread.
            _ => notifier
                .request_permission()
                .map(|_| ())
                .map_err(|err| err.to_string()),
        }
    })
    .await;
    match result {
        Ok(Err(err)) => log::warn!("notification permission request failed: {err}"),
        Err(err) => log::error!("notification permission task failed: {err}"),
        Ok(Ok(())) => {}
    }
}

/// Show an Android notification without synchronously invoking its plugin.
pub async fn show_notification<R: Runtime>(app: AppHandle<R>, title: String, body: String) {
    let result = tauri::async_runtime::spawn_blocking(move || {
        use tauri_plugin_notification::NotificationExt;

        // NotificationBuilder::show also uses run_mobile_plugin internally.
        app.notification()
            .builder()
            .title(&title)
            .body(&body)
            .show()
            .map_err(|err| err.to_string())
    })
    .await;
    match result {
        Ok(Err(err)) => log::error!("could not show a notification: {err}"),
        Err(err) => log::error!("notification task failed: {err}"),
        Ok(Ok(())) => {}
    }
}

/// Startup wiring: restore the persisted notification permission and service
/// choices. Fresh installs default both off, so onboarding happens first.
pub fn setup<R: Runtime>(app: &AppHandle<R>) {
    let settings = app.state::<crate::state::ShellState>().settings();
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        // Prime the cache asynchronously. Window startup uses the same loader
        // before it attempts auto-login, so no main-thread JNI call is needed.
        let _ = load_stored_secret(&handle).await;
        if settings.notifications {
            request_notification_permission_async(handle.clone()).await;
        }
        if settings.background_service {
            set_background_service_async(&handle, true).await;
        }
    });
}
