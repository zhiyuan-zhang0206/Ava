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
const PLUGIN_CLASS: &str = "AvaBackgroundPlugin";
const PLUGIN_NAME: &str = "avabackground";

/// Managed handle to the Kotlin side. Absent when the plugin failed to load,
/// which must degrade to "no background residency", never to a failed launch.
struct BackgroundPlugin<R: Runtime>(PluginHandle<R>);

/// Register the Kotlin foreground-service plugin.
pub fn background_plugin<R: Runtime>() -> TauriPlugin<R> {
    PluginBuilder::new(PLUGIN_NAME)
        .setup(|app, api| {
            match api.register_android_plugin(PLUGIN_PACKAGE, PLUGIN_CLASS) {
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
