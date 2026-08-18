// The command list is shared with the crate rather than retyped: `tauri-build`
// turns it into the `allow-<command>` permissions the capabilities reference,
// and `lib.rs` registers the same names. Two copies would drift into a
// "command not allowed by ACL" that looks like a bug in the webview.
include!("src/command_names.rs");

fn main() {
    let attributes = tauri_build::Attributes::new()
        .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS));
    tauri_build::try_build(attributes).expect("failed to run tauri-build");
}
