// The shell's IPC command names.
//
// This file is BOTH a module of the crate and `include!`d by `build.rs`, so the
// list that `tauri-build` turns into `allow-*` permissions is the same list the
// handler registers. Keep it free of anything but this constant — build scripts
// cannot see the rest of the crate.

/// Every `#[tauri::command]` the shell exposes.
///
/// The real consumer is `build.rs`, which the compiler cannot see from inside
/// the crate; the crate itself only reads it from the capability drift tests.
#[allow(dead_code)]
pub const COMMANDS: &[&str] = &[
    "shell_config",
    "shell_open_external",
    "shell_cluster_secret",
    "shell_save_settings",
    "shell_retry_entry",
    "shell_open_settings",
    "shell_notify",
];
