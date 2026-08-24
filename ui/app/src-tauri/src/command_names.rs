// The app's IPC command names.
//
// This file is BOTH a module of the crate and `include!`d by `build.rs`, so the
// list that `tauri-build` turns into `allow-*` permissions is the same list the
// handler registers. Keep it free of anything but this constant — build scripts
// cannot see the rest of the crate.

/// Every `#[tauri::command]` the app exposes.
///
/// The real consumer is `build.rs`, which the compiler cannot see from inside
/// the crate; the crate itself only reads it from the capability drift tests.
#[allow(dead_code)]
pub const COMMANDS: &[&str] = &[
    "app_config",
    "app_open_external",
    "app_save_settings",
    "app_retry_entry",
    "app_open_settings",
    "app_notify",
    "app_take_pending_click",
];
