//! Shell state shared between the Tauri commands and the window plumbing.

use std::path::PathBuf;
use std::sync::Mutex;

use crate::settings::Settings;
use crate::urls::{self, Endpoints};

/// The one piece of managed state: the settings file's location and its
/// in-memory image. Everything else (endpoints, injected script config) is
/// derived so there is no second copy to keep in sync.
pub struct ShellState {
    config_dir: PathBuf,
    settings: Mutex<Settings>,
}

impl ShellState {
    pub fn new(config_dir: PathBuf, settings: Settings) -> Self {
        Self {
            config_dir,
            settings: Mutex::new(settings),
        }
    }

    pub fn settings(&self) -> Settings {
        self.settings.lock().expect("settings lock").clone()
    }

    /// Resolved gate + gateway, or `None` when no server has been configured
    /// (Android before onboarding).
    pub fn endpoints(&self) -> Option<Endpoints> {
        urls::resolve_checked(&self.settings(), cfg!(target_os = "android"))
    }

    /// Replace the settings and persist them. The caller decides what to do
    /// with the window afterwards (usually navigate to the new entry URL).
    pub fn update(&self, next: Settings) -> Result<(), String> {
        next.save(&self.config_dir)?;
        *self.settings.lock().expect("settings lock") = next;
        Ok(())
    }
}
