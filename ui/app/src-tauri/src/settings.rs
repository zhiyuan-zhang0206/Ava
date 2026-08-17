//! Shell settings — `settings.json` in the platform config directory.
//!
//! The file carries the desktop connection keys (`entryUrl`, `gatewayUrl`,
//! `autoLogin`) plus the two Android residency switches. Every key is optional: a
//! missing or unparseable file falls back to [`Settings::default`] rather than
//! failing the launch — a shell that cannot start is worse than a shell on
//! defaults.

use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// Desktop entry default — the always-up gate, which serves the login page when
/// signed out and proxies the console when signed in. Android has no default:
/// a phone is never the machine the cluster runs on, so it must be told.
#[cfg(not(target_os = "android"))]
pub const DEFAULT_ENTRY_URL: &str = "http://localhost:3000";

/// File name inside the app config directory.
const FILE_NAME: &str = "settings.json";

/// Persisted shell configuration.
///
/// `entry_url` is `None` on Android until first-run onboarding stores one; on
/// desktop it defaults to the local gate. `gateway_url` is `None` unless the
/// user pins it — otherwise it is derived from the entry URL the same way the
/// web console derives `API_BASE` (same host, gateway port).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Settings {
    pub entry_url: Option<String>,
    pub gateway_url: Option<String>,
    /// Desktop only: log in with the local cluster secret when one exists.
    pub auto_login: bool,
    /// Android only: keep a foreground service alive so the console stays
    /// connected while the app is backgrounded.
    pub background_service: bool,
    /// Android only: raise local notifications for the conservative event
    /// subset (agent finished / agent needs input / FYI notice).
    pub notifications: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            // Android has no meaningful default server — the first run asks.
            #[cfg(target_os = "android")]
            entry_url: None,
            #[cfg(not(target_os = "android"))]
            entry_url: Some(DEFAULT_ENTRY_URL.to_string()),
            gateway_url: None,
            auto_login: true,
            // Mobile residency and notifications are opt-in on onboarding;
            // in particular, do not raise Android's permission prompt before
            // the user has seen the switch that explains it.
            background_service: false,
            notifications: false,
        }
    }
}

impl Settings {
    /// Load `settings.json` from `dir`, falling back to defaults when the file
    /// is missing or malformed.
    pub fn load(dir: &Path) -> Self {
        let path = dir.join(FILE_NAME);
        let Ok(text) = fs::read_to_string(&path) else {
            return Self::default();
        };
        match serde_json::from_str(&text) {
            Ok(settings) => settings,
            Err(err) => {
                log::warn!("settings.json is not valid JSON ({err}); using defaults");
                Self::default()
            }
        }
    }

    /// Write `settings.json` into `dir`, creating the directory if needed.
    pub fn save(&self, dir: &Path) -> Result<(), String> {
        fs::create_dir_all(dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
        let path = dir.join(FILE_NAME);
        let text = serde_json::to_string_pretty(self).map_err(|e| e.to_string())?;
        fs::write(&path, text).map_err(|e| format!("cannot write {}: {e}", path.display()))
    }
}

/// Where `settings.json` lives. Resolved from the Tauri path API so each
/// platform lands in its own convention (`~/Library/Application Support/…`,
/// `%APPDATA%\…`, the Android app data dir).
pub fn config_dir<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> PathBuf {
    use tauri::Manager;
    app.path()
        .app_config_dir()
        .expect("the platform must expose an app config directory")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_file_yields_defaults() {
        let dir = std::env::temp_dir().join("ava-shell-settings-missing");
        let _ = fs::remove_dir_all(&dir);
        assert_eq!(Settings::load(&dir), Settings::default());
    }

    #[test]
    fn mobile_features_are_opt_in_by_default() {
        let settings = Settings::default();
        assert!(!settings.background_service);
        assert!(!settings.notifications);
    }

    #[test]
    fn round_trips_through_disk() {
        let dir = std::env::temp_dir().join("ava-shell-settings-roundtrip");
        let _ = fs::remove_dir_all(&dir);
        let settings = Settings {
            entry_url: Some("http://box:3000".into()),
            gateway_url: Some("http://box:8123".into()),
            auto_login: false,
            background_service: false,
            notifications: false,
        };
        settings.save(&dir).expect("save");
        assert_eq!(Settings::load(&dir), settings);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn partial_json_keeps_defaults_for_absent_keys() {
        let dir = std::env::temp_dir().join("ava-shell-settings-partial");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("mkdir");
        fs::write(dir.join(FILE_NAME), r#"{"autoLogin": false}"#).expect("write");
        let loaded = Settings::load(&dir);
        assert!(!loaded.auto_login);
        assert_eq!(loaded.gateway_url, Settings::default().gateway_url);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn malformed_json_falls_back_instead_of_panicking() {
        let dir = std::env::temp_dir().join("ava-shell-settings-malformed");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("mkdir");
        fs::write(dir.join(FILE_NAME), "{ not json").expect("write");
        assert_eq!(Settings::load(&dir), Settings::default());
        let _ = fs::remove_dir_all(&dir);
    }
}
