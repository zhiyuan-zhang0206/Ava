//! Desktop residency: the tray, the close-hides-to-tray rule, and updates.
//!
//! The shell is meant to sit in the tray all day. Closing the window hides it;
//! the only real exit is the tray's Quit (or Cmd+Q), keeping the console one
//! click away.

use tauri::menu::{CheckMenuItem, Menu, MenuEvent, MenuItem, PredefinedMenuItem};
use tauri::tray::{TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, WebviewWindow};

use crate::window::{self, MAIN_WINDOW};

/// Menu-bar icon. Black-on-transparent so macOS can treat it as a template;
/// Windows renders it as-is.
const TRAY_ICON: &[u8] = include_bytes!("../icons/tray.png");

const MENU_OPEN: &str = "open";
const MENU_AUTOSTART: &str = "autostart";
const MENU_UPDATE: &str = "update";
const MENU_QUIT: &str = "quit";

/// Bring the window back from the tray.
pub fn show_main_window(app: &AppHandle) {
    match app.get_webview_window(MAIN_WINDOW) {
        Some(window) => {
            let _ = window.unminimize();
            let _ = window.show();
            let _ = window.set_focus();
        }
        // The window is only ever absent if a rebuild failed; recreate it
        // rather than leaving the tray icon controlling nothing.
        None => window::open_entry(app),
    }
}

/// Close hides instead of quitting — the shell stays resident.
pub fn attach_window_behavior(window: &WebviewWindow) {
    let handle = window.clone();
    window.on_window_event(move |event| {
        if let tauri::WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();
            let _ = handle.hide();
        }
    });
}

/// Build the tray icon and its menu.
pub fn setup_tray(app: &AppHandle) -> tauri::Result<()> {
    let autostart_on = autostart_enabled(app);
    let open = MenuItem::with_id(app, MENU_OPEN, "Open Ava", true, None::<&str>)?;
    let autostart = CheckMenuItem::with_id(
        app,
        MENU_AUTOSTART,
        "Launch at login",
        true,
        autostart_on,
        None::<&str>,
    )?;
    let update = MenuItem::with_id(app, MENU_UPDATE, "Check for updates…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, MENU_QUIT, "Quit Ava", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &open,
            &PredefinedMenuItem::separator(app)?,
            &autostart,
            &update,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )?;

    TrayIconBuilder::with_id("ava")
        // A dedicated monochrome template, not the app icon: macOS renders a
        // template as a silhouette that follows the menu bar's light/dark
        // appearance, and a full-colour icon squashed to 18pt reads as a blob.
        .icon(tauri::image::Image::from_bytes(TRAY_ICON)?)
        .icon_as_template(true)
        .tooltip("Ava")
        .menu(&menu)
        // The menu is the tray's whole surface on Windows; on macOS a left
        // click should still just open the window.
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button, .. } = event {
                if button == tauri::tray::MouseButton::Left {
                    show_main_window(tray.app_handle());
                }
            }
        })
        .on_menu_event(on_menu_event)
        .build(app)?;
    Ok(())
}

fn on_menu_event(app: &AppHandle, event: MenuEvent) {
    match event.id().as_ref() {
        MENU_OPEN => show_main_window(app),
        MENU_AUTOSTART => toggle_autostart(app),
        MENU_UPDATE => check_for_updates(app.clone(), true),
        MENU_QUIT => app.exit(0),
        other => log::warn!("unknown tray menu item '{other}'"),
    }
}

fn autostart_enabled(app: &AppHandle) -> bool {
    use tauri_plugin_autostart::ManagerExt;
    app.autolaunch().is_enabled().unwrap_or(false)
}

fn toggle_autostart(app: &AppHandle) {
    use tauri_plugin_autostart::ManagerExt;
    let manager = app.autolaunch();
    let result = if autostart_enabled(app) {
        manager.disable()
    } else {
        manager.enable()
    };
    if let Err(err) = result {
        log::error!("could not change the launch-at-login setting: {err}");
    }
}

/// Ask the updater for a newer release and install it when one exists.
///
/// `interactive` distinguishes the tray's "Check for updates…" from the silent
/// startup check: only the former is worth surfacing when nothing is found.
pub fn check_for_updates(app: AppHandle, interactive: bool) {
    tauri::async_runtime::spawn(async move {
        use tauri_plugin_updater::UpdaterExt;

        let updater = match app.updater() {
            Ok(updater) => updater,
            Err(err) => {
                log::error!("updater unavailable: {err}");
                return;
            }
        };
        match updater.check().await {
            Ok(Some(update)) => {
                let version = update.version.clone();
                log::info!("installing update {version}");
                match update.download_and_install(|_, _| {}, || {}).await {
                    // The new binary only runs after a restart; the user is
                    // mid-session, so ask rather than yanking the window away.
                    Ok(()) => log::info!("update {version} staged — restart Ava to run it"),
                    Err(err) => log::error!("could not install update {version}: {err}"),
                }
            }
            Ok(None) => {
                if interactive {
                    log::info!("no update available");
                }
            }
            Err(err) => log::error!("update check failed: {err}"),
        }
    });
}
