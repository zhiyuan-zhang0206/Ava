//! The IPC surface the injected scripts and the bundled pages call.
//!
//! Every command is deliberately small and side-effect-explicit. Remote pages
//! (the console itself) reach these only through the runtime capability the
//! app grants for the configured gate origin — see `window::grant_remote_ipc`.

use serde::{Deserialize, Serialize};
use tauri::Manager;
use tauri::{AppHandle, Runtime, State};

use crate::state::AppState;
use crate::{external, window};

/// What the injected prelude publishes as `window.__AVA_APP__`, and what the
/// bundled pages read back through `app_config`.
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AppConfig {
    pub version: String,
    pub platform: &'static str,
    pub entry_url: Option<String>,
    pub gateway_url: Option<String>,
    /// True only when this platform has a usable native-login secret.
    pub auto_login: bool,
    pub notifications: bool,
    pub background_service: bool,
    /// GitHub Releases API listing used by the Android update check; `None`
    /// on desktop, where `tauri-plugin-updater` owns updates instead.
    pub releases_api: Option<String>,
}

/// Releases listing for the app's own tags. Desktop uses the updater plugin,
/// so only Android consults this.
const RELEASES_API: &str = "https://api.github.com/repos/zhiyuan-zhang0206/Ava/releases";

impl AppConfig {
    pub fn build<R: Runtime>(app: &AppHandle<R>, state: &AppState) -> Self {
        let settings = state.settings();
        let endpoints = state.endpoints();
        Self {
            version: app.package_info().version.to_string(),
            platform: std::env::consts::OS,
            entry_url: endpoints.as_ref().map(|e| e.entry.to_string()),
            gateway_url: endpoints.as_ref().map(|e| e.gateway.to_string()),
            auto_login: {
                #[cfg(desktop)]
                {
                    settings.auto_login && crate::autologin::cluster_secret().is_some()
                }
                #[cfg(target_os = "android")]
                {
                    state.android_secret().is_some()
                }
                #[cfg(not(any(desktop, target_os = "android")))]
                {
                    false
                }
            },
            notifications: cfg!(target_os = "android") && settings.notifications,
            background_service: cfg!(target_os = "android") && settings.background_service,
            releases_api: cfg!(target_os = "android").then(|| RELEASES_API.to_string()),
        }
    }
}

/// Preserve desktop's existing arbitrary-address behavior while Android's
/// onboarding field identifies a cluster and therefore normalizes its ports.
fn entry_url_for_save(raw: &str) -> Result<String, String> {
    #[cfg(target_os = "android")]
    {
        crate::urls::normalize_entry_address(raw)
    }
    #[cfg(not(target_os = "android"))]
    {
        crate::urls::parse_entry(raw).ok_or_else(|| format!("'{raw}' is not a server address"))?;
        Ok(raw.to_string())
    }
}

/// Match the entry-address compatibility boundary for the optional gateway
/// override: Android normalizes onboarding input; desktop keeps prior values.
fn gateway_url_for_save(raw: &str) -> Result<String, String> {
    #[cfg(target_os = "android")]
    {
        crate::urls::normalize_gateway_address(raw)
    }
    #[cfg(not(target_os = "android"))]
    {
        crate::urls::parse_entry(raw).ok_or_else(|| format!("'{raw}' is not a server address"))?;
        Ok(raw.to_string())
    }
}

/// Fields the bundled onboarding page may change. Absent fields are untouched,
/// so the page never has to round-trip settings it does not own.
#[derive(Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct SettingsPatch {
    pub entry_url: Option<String>,
    pub gateway_url: Option<String>,
    /// Android only: optional first-login credential; never persisted here.
    pub cluster_secret: Option<String>,
    pub auto_login: Option<bool>,
    pub background_service: Option<bool>,
    pub notifications: Option<bool>,
}

#[tauri::command]
pub fn app_config(app: AppHandle, state: State<'_, AppState>) -> AppConfig {
    AppConfig::build(&app, &state)
}

#[tauri::command]
pub fn app_open_external(app: AppHandle, url: String) -> Result<(), String> {
    external::open_external(&app, &url)
}

/// Refuse a cleartext server address outside private network space.
///
/// This is the range policy Android's network security config is meant to
/// carry and cannot express (`ui/app/android/network_security_config.xml`), so
/// it lives here instead. An operator pointing the desktop app at an address
/// is not the threat model this
/// guards, and localhost is the overwhelming case there.
fn check_cleartext_target(url: &tauri::Url) -> Result<(), String> {
    #[cfg(target_os = "android")]
    if !crate::urls::is_private_host(url) {
        return Err(format!(
            "{url} is not on a private network — plain HTTP would send your \
             session cookie in the clear. Use a private address or https."
        ));
    }
    #[cfg(not(target_os = "android"))]
    let _ = url;
    Ok(())
}

/// Persist a settings patch and queue the resulting entry-window update.
#[tauri::command]
pub async fn app_save_settings(
    app: AppHandle,
    state: State<'_, AppState>,
    patch: SettingsPatch,
) -> Result<(), String> {
    let mut settings = state.settings();
    if let Some(raw) = patch.entry_url {
        // Validate before persisting: a stored address that cannot be parsed
        // would strand the app on the onboarding screen with no way back.
        let entry_url = entry_url_for_save(&raw)?;
        let url = crate::urls::parse_entry(&entry_url)
            .ok_or_else(|| format!("'{entry_url}' is not a server address"))?;
        check_cleartext_target(&url)?;
        settings.entry_url = Some(entry_url);
    }
    if let Some(raw) = patch.gateway_url {
        if raw.trim().is_empty() {
            settings.gateway_url = None;
        } else {
            let gateway_url = gateway_url_for_save(&raw)?;
            let url = crate::urls::parse_entry(&gateway_url)
                .ok_or_else(|| format!("'{gateway_url}' is not a server address"))?;
            check_cleartext_target(&url)?;
            settings.gateway_url = Some(gateway_url);
        }
    }
    if let Some(value) = patch.auto_login {
        settings.auto_login = value;
    }
    if let Some(value) = patch.background_service {
        settings.background_service = value;
    }
    if let Some(value) = patch.notifications {
        settings.notifications = value;
    }
    state.update(settings)?;
    #[cfg(target_os = "android")]
    {
        if let Some(secret) = patch.cluster_secret {
            if secret.is_empty() {
                crate::android::clear_secret_async(&app).await?;
                state.cache_android_secret(None);
            } else {
                let endpoints = state
                    .endpoints()
                    .ok_or_else(|| "saved server address cannot be resolved".to_string())?;
                crate::autologin::login(&app, &endpoints, &secret)
                    .await
                    .map_err(|err| err.message())?;
                // Keep a submitted credential only after its native login has
                // succeeded; settings.json never receives this value.
                crate::android::save_secret_async(&app, &secret).await?;
                state.cache_android_secret(Some(secret));
                state.skip_next_android_autologin();
            }
        }
        if let Some(value) = patch.background_service {
            crate::android::set_background_service_async(&app, value).await;
        }
        if patch.notifications == Some(true) {
            crate::android::request_notification_permission_async(app.clone()).await;
        }
    }
    let handle = app.clone();
    // Deferring lets the invoke response flush before the window changes.
    // Android refreshes its prelude then navigates the responding webview;
    // desktop keeps its existing main-thread rebuild.
    tauri::async_runtime::spawn(async move {
        #[cfg(target_os = "android")]
        window::open_entry(&handle);
        #[cfg(not(target_os = "android"))]
        {
            let window_handle = handle.clone();
            if let Err(err) = handle.run_on_main_thread(move || window::open_entry(&window_handle))
            {
                log::error!("could not schedule the entry-window rebuild: {err}");
            }
        }
    });
    Ok(())
}

/// Restart the entry-load retry loop (the "Try again" button).
#[tauri::command]
pub fn app_retry_entry(app: AppHandle) {
    window::open_entry(&app);
}

/// Open the bundled settings screen. Remote console pages can ask to show this
/// page but cannot persist settings themselves; only the bundled origin has
/// `app_save_settings` permission.
#[tauri::command]
pub fn app_open_settings(app: AppHandle) {
    window::open_settings(&app);
}

/// Raise a local notification. Android only — on desktop the console window is
/// already the notification surface, so this logs and returns.
#[tauri::command]
pub async fn app_notify(
    app: AppHandle,
    state: State<'_, AppState>,
    title: String,
    body: String,
) -> Result<(), String> {
    #[cfg(target_os = "android")]
    {
        if !state.settings().notifications {
            return Ok(());
        }
        let title = title.chars().take(120).collect::<String>();
        let body = body.chars().take(500).collect::<String>();
        crate::android::show_notification(app, title, body).await;
    }
    #[cfg(not(target_os = "android"))]
    {
        let _ = (&app, &state, &title, &body);
        log::debug!("notification suppressed off Android: {title}");
    }
    Ok(())
}

/// Consume an Android notification tap once the authenticated console is ready.
#[tauri::command]
pub async fn app_take_pending_click(app: AppHandle) -> bool {
    if !app.state::<AppState>().settings().notifications {
        return false;
    }
    #[cfg(target_os = "android")]
    {
        crate::android::take_pending_click(&app)
            .await
            .unwrap_or(false)
    }
    #[cfg(not(target_os = "android"))]
    {
        let _ = app;
        log::debug!("notification-click capture suppressed off Android");
        false
    }
}

#[cfg(test)]
mod tests {
    use super::{entry_url_for_save, gateway_url_for_save};
    use crate::command_names::COMMANDS;
    use crate::window::REMOTE_PERMISSIONS;

    /// The static capability, read as text: a command is reachable from the
    /// app's own pages only if its `allow-*` permission is listed there.
    const LOCAL_CAPABILITY: &str = include_str!("../capabilities/local.json");

    /// `tauri-build` derives permission identifiers by kebab-casing the command.
    fn permission_of(command: &str) -> String {
        format!("allow-{}", command.replace('_', "-"))
    }

    /// A command nobody is allowed to call is dead IPC — usually a command
    /// added to `command_names.rs` and to the handler, but never granted. The
    /// symptom in the app is a silent "not allowed by ACL" rejection inside a
    /// webview, which is expensive to trace back to a missing capability line.
    #[test]
    fn every_command_is_reachable_from_at_least_one_surface() {
        for command in COMMANDS {
            let permission = permission_of(command);
            let granted = LOCAL_CAPABILITY.contains(&permission)
                || REMOTE_PERMISSIONS.contains(&permission.as_str());
            assert!(
                granted,
                "{command} is registered but no capability grants it"
            );
        }
    }

    /// The reverse direction: a capability naming a command that no longer
    /// exists fails the build under `tauri-build`'s validation, but the remote
    /// list is assembled at runtime and would only fail on a live device.
    #[test]
    fn the_remote_grant_names_only_real_commands() {
        let known: Vec<String> = COMMANDS.iter().map(|c| permission_of(c)).collect();
        for permission in REMOTE_PERMISSIONS {
            assert!(
                known.iter().any(|k| k == permission),
                "{permission} is granted to the console but matches no command"
            );
        }
    }

    #[test]
    fn no_ipc_command_can_read_the_cluster_secret() {
        assert!(!COMMANDS.iter().any(|command| command.contains("secret")));
    }

    #[cfg(not(target_os = "android"))]
    #[test]
    fn desktop_keeps_legacy_custom_entry_and_gateway_addresses() {
        assert_eq!(
            entry_url_for_save("http://worktree.local:3100/api"),
            Ok("http://worktree.local:3100/api".to_string())
        );
        assert_eq!(
            entry_url_for_save("http://worktree.local:8000"),
            Ok("http://worktree.local:8000".to_string())
        );
        assert_eq!(
            gateway_url_for_save("http://worktree.local:3000/api"),
            Ok("http://worktree.local:3000/api".to_string())
        );
    }
}
