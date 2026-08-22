//! Shell state shared between the Tauri commands and the window plumbing.

use std::path::PathBuf;
#[cfg(any(target_os = "android", test))]
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use crate::settings::Settings;
use crate::urls::{self, Endpoints};

/// Managed shell state: persisted settings plus transient Android-native state.
///
/// The optional Android secret cache mirrors the Keystore result only in
/// process memory. It lets window construction report login capability without
/// making a synchronous JNI call on the Android main thread.
pub struct ShellState {
    config_dir: PathBuf,
    settings: Mutex<Settings>,
    #[cfg(any(target_os = "android", test))]
    android_secret: Mutex<Option<String>>,
    #[cfg(any(target_os = "android", test))]
    android_secret_loaded: AtomicBool,
    #[cfg(any(target_os = "android", test))]
    skip_next_android_autologin: AtomicBool,
}

impl ShellState {
    pub fn new(config_dir: PathBuf, settings: Settings) -> Self {
        Self {
            config_dir,
            settings: Mutex::new(settings),
            #[cfg(any(target_os = "android", test))]
            android_secret: Mutex::new(None),
            #[cfg(any(target_os = "android", test))]
            android_secret_loaded: AtomicBool::new(false),
            #[cfg(any(target_os = "android", test))]
            skip_next_android_autologin: AtomicBool::new(false),
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

    /// Whether the asynchronous Android Keystore load has completed.
    #[cfg(any(target_os = "android", test))]
    pub fn android_secret_loaded(&self) -> bool {
        self.android_secret_loaded.load(Ordering::Acquire)
    }

    /// The Android Keystore value cached after an asynchronous native read.
    #[cfg(any(target_os = "android", test))]
    pub fn android_secret(&self) -> Option<String> {
        self.android_secret_loaded()
            .then(|| {
                self.android_secret
                    .lock()
                    .expect("Android secret lock")
                    .clone()
            })
            .flatten()
    }

    /// Replace the process-local mirror after a successful native load/save/clear.
    #[cfg(any(target_os = "android", test))]
    pub fn cache_android_secret(&self, secret: Option<String>) {
        *self.android_secret.lock().expect("Android secret lock") = secret;
        self.android_secret_loaded.store(true, Ordering::Release);
    }

    /// Cache an asynchronous Keystore read only when no save/clear has won
    /// the race in the meantime. Returns the cache value that remains valid.
    #[cfg(any(target_os = "android", test))]
    pub fn cache_android_secret_if_unloaded(&self, secret: Option<String>) -> Option<String> {
        let mut cached = self.android_secret.lock().expect("Android secret lock");
        if self
            .android_secret_loaded
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
        {
            *cached = secret.clone();
            return secret;
        }
        cached.clone()
    }

    /// Suppress exactly one Android startup login after onboarding already
    /// installed the session cookie in the window that is about to rebuild.
    #[cfg(any(target_os = "android", test))]
    pub fn skip_next_android_autologin(&self) {
        self.skip_next_android_autologin
            .store(true, Ordering::Release);
    }

    /// Consume the post-save Android auto-login suppression exactly once.
    #[cfg(any(target_os = "android", test))]
    pub fn take_skip_next_android_autologin(&self) -> bool {
        self.skip_next_android_autologin
            .swap(false, Ordering::AcqRel)
    }
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::ShellState;
    use crate::settings::Settings;

    #[test]
    fn android_autologin_skip_is_consumed_once() {
        let state = ShellState::new(PathBuf::new(), Settings::default());

        assert!(!state.take_skip_next_android_autologin());
        state.skip_next_android_autologin();
        assert!(state.take_skip_next_android_autologin());
        assert!(!state.take_skip_next_android_autologin());
    }

    #[test]
    fn android_secret_cache_tracks_when_native_loading_finished() {
        let state = ShellState::new(PathBuf::new(), Settings::default());

        assert!(!state.android_secret_loaded());
        assert_eq!(state.android_secret(), None);

        state.cache_android_secret(Some("stored-secret".to_string()));

        assert!(state.android_secret_loaded());
        assert_eq!(state.android_secret(), Some("stored-secret".to_string()));
    }

    #[test]
    fn late_android_secret_load_cannot_overwrite_a_newly_saved_secret() {
        let state = ShellState::new(PathBuf::new(), Settings::default());
        state.cache_android_secret(Some("newly-saved".to_string()));

        let retained = state.cache_android_secret_if_unloaded(None);

        assert_eq!(retained, Some("newly-saved".to_string()));
        assert_eq!(state.android_secret(), Some("newly-saved".to_string()));
    }
}
