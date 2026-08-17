//! The IPC surface the injected scripts and the bundled pages call.
//!
//! Every command is deliberately small and side-effect-explicit. Remote pages
//! (the console itself) reach these only through the runtime capability the
//! shell grants for the configured gate origin — see `window::grant_remote_ipc`.

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Runtime, State};

use crate::state::ShellState;
use crate::{external, window};

/// What the injected prelude publishes as `window.__AVA_SHELL__`, and what the
/// bundled pages read back through `shell_config`.
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ShellConfig {
    pub version: String,
    pub platform: &'static str,
    pub entry_url: Option<String>,
    pub gateway_url: Option<String>,
    /// True only where auto-login can actually happen: desktop, setting on.
    pub auto_login: bool,
    pub notifications: bool,
    pub background_service: bool,
    /// GitHub Releases API listing used by the Android update check; `None`
    /// on desktop, where `tauri-plugin-updater` owns updates instead.
    pub releases_api: Option<String>,
}

/// Releases listing for the shell's own tags. Desktop uses the updater plugin,
/// so only Android consults this.
const RELEASES_API: &str = "https://api.github.com/repos/zhiyuan-zhang0206/Ava/releases";

impl ShellConfig {
    pub fn build<R: Runtime>(app: &AppHandle<R>, state: &ShellState) -> Self {
        let settings = state.settings();
        let endpoints = state.endpoints();
        Self {
            version: app.package_info().version.to_string(),
            platform: std::env::consts::OS,
            entry_url: endpoints.as_ref().map(|e| e.entry.to_string()),
            gateway_url: endpoints.as_ref().map(|e| e.gateway.to_string()),
            auto_login: cfg!(desktop) && settings.auto_login,
            notifications: cfg!(target_os = "android") && settings.notifications,
            background_service: cfg!(target_os = "android") && settings.background_service,
            releases_api: cfg!(target_os = "android").then(|| RELEASES_API.to_string()),
        }
    }
}

/// Fields the bundled onboarding page may change. Absent fields are untouched,
/// so the page never has to round-trip settings it does not own.
#[derive(Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct SettingsPatch {
    pub entry_url: Option<String>,
    pub gateway_url: Option<String>,
    pub auto_login: Option<bool>,
    pub background_service: Option<bool>,
    pub notifications: Option<bool>,
}

#[tauri::command]
pub fn shell_config(app: AppHandle, state: State<'_, ShellState>) -> ShellConfig {
    ShellConfig::build(&app, &state)
}

#[tauri::command]
pub fn shell_open_external(app: AppHandle, url: String) -> Result<(), String> {
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

/// Persist a settings patch and re-point the window at the resulting entry URL.
#[tauri::command]
pub fn shell_save_settings(
    app: AppHandle,
    state: State<'_, ShellState>,
    patch: SettingsPatch,
) -> Result<(), String> {
    let mut settings = state.settings();
    if let Some(raw) = patch.entry_url {
        // Validate before persisting: a stored address that cannot be parsed
        // would strand the app on the onboarding screen with no way back.
        let url = crate::urls::parse_entry(&raw)
            .ok_or_else(|| format!("'{raw}' is not a server address"))?;
        check_cleartext_target(&url)?;
        settings.entry_url = Some(raw);
    }
    if let Some(raw) = patch.gateway_url {
        if raw.trim().is_empty() {
            settings.gateway_url = None;
        } else {
            let url = crate::urls::parse_entry(&raw)
                .ok_or_else(|| format!("'{raw}' is not a server address"))?;
            check_cleartext_target(&url)?;
            settings.gateway_url = Some(raw);
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
        if let Some(value) = patch.background_service {
            crate::android::set_background_service(&app, value);
        }
        if patch.notifications == Some(true) {
            crate::android::request_notification_permission(&app);
        }
    }
    window::open_entry(&app);
    Ok(())
}

/// Restart the entry-load retry loop (the "Try again" button).
#[tauri::command]
pub fn shell_retry_entry(app: AppHandle) {
    window::open_entry(&app);
}

/// Open the bundled settings screen. Remote console pages can ask to show this
/// page but cannot persist settings themselves; only the bundled origin has
/// `shell_save_settings` permission.
#[tauri::command]
pub fn shell_open_settings(app: AppHandle) {
    window::open_settings(&app);
}

/// Raise a local notification. Android only — on desktop the console window is
/// already the notification surface, so this logs and returns.
#[tauri::command]
pub fn shell_notify(app: AppHandle, state: State<'_, ShellState>, title: String, body: String) {
    #[cfg(target_os = "android")]
    {
        use tauri_plugin_notification::NotificationExt;
        if !state.settings().notifications {
            return;
        }
        let title = title.chars().take(120).collect::<String>();
        let body = body.chars().take(500).collect::<String>();
        if let Err(err) = app
            .notification()
            .builder()
            .title(&title)
            .body(&body)
            .show()
        {
            log::error!("could not show a notification: {err}");
        }
    }
    #[cfg(not(target_os = "android"))]
    {
        let _ = (&app, &state, &title, &body);
        log::debug!("notification suppressed off Android: {title}");
    }
}

#[cfg(test)]
mod tests {
    use crate::command_names::COMMANDS;
    use crate::window::REMOTE_PERMISSIONS;

    /// The static capability, read as text: a command is reachable from the
    /// shell's own pages only if its `allow-*` permission is listed there.
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
}
