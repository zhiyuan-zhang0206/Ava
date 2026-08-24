//! Ava App — one Tauri application for macOS, Windows and Android.
//!
//! The app renders no product UI of its own: it loads the cluster's console
//! over the network and adds only what a browser tab cannot give it — tray
//! residency and auto-login on desktop, Keystore-backed auto-login, background
//! residency and local notifications on Android, and an update path on both.
//! Everything the user actually looks at is served by `ui/web` behind the gate.

#[cfg(target_os = "android")]
mod android;
mod autologin;
mod command_names;
mod commands;
#[cfg(desktop)]
mod desktop;
mod external;
mod settings;
mod state;
mod urls;
mod window;

use tauri::Manager;

use crate::settings::Settings;
use crate::state::AppState;

/// Application entry point, shared by the desktop binary and the Android
/// library entry `tauri::mobile_entry_point` generates.
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default().plugin(tauri_plugin_opener::init());

    #[cfg(desktop)]
    let builder = builder
        // A second launch focuses the running app instead of opening a
        // second console window against the same cluster.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            desktop::show_main_window(app);
        }))
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ));

    #[cfg(target_os = "android")]
    let builder = builder
        .plugin(tauri_plugin_notification::init())
        .plugin(android::background_plugin())
        .plugin(android::secret_plugin())
        .plugin(android::click_plugin());

    builder
        .invoke_handler(tauri::generate_handler![
            commands::app_config,
            commands::app_open_external,
            commands::app_save_settings,
            commands::app_retry_entry,
            commands::app_open_settings,
            commands::app_notify,
            commands::app_take_pending_click,
        ])
        .setup(|app| {
            let handle = app.handle();
            let config_dir = settings::config_dir(handle);
            let settings = Settings::load(&config_dir);
            app.manage(AppState::new(config_dir, settings));

            window::open_entry(handle);

            #[cfg(desktop)]
            {
                desktop::setup_tray(handle)?;
                desktop::check_for_updates(handle.clone(), false);
            }

            #[cfg(target_os = "android")]
            android::setup(handle);

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("the app context must be valid")
        .run(|_app, event| {
            // Tray residency: the last window closing is not a reason to exit.
            // Only the tray's Quit (app.exit) ends the process.
            if let tauri::RunEvent::ExitRequested { api, code, .. } = event {
                if code.is_none() {
                    api.prevent_exit();
                }
            }
        });
}
