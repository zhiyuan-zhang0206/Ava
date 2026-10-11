// Desktop binary entry. Android loads the library through
// `tauri::mobile_entry_point` instead, so all real wiring lives in `lib.rs`.

fn main() {
    ava_app_lib::run()
}
