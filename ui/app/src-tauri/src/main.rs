// Desktop binary entry. Android loads the library through
// `tauri::mobile_entry_point` instead, so all real wiring lives in `lib.rs`.

// Release builds must not open a console window behind the app on Windows.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    ava_app_lib::run()
}
