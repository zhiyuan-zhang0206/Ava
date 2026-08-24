//! External links leave the app.
//!
//! The app has no tabs, so every link that is not the console itself is
//! handed off through this fallback chain:
//!
//! 1. the cluster's own headed Chrome (`ava-browser`), when its browser-MCP
//!    unix socket is present locally — the link lands in the browser the agents
//!    already drive, logged into the sites the operator signed into;
//! 2. otherwise the system default browser.
//!
//! No cross-machine forwarding: a link is opened on the machine that clicked it.

use tauri::Url;
use tauri_plugin_opener::OpenerExt;

/// Open `raw` outside the app without holding the webview's UI thread while
/// the browser-MCP fallback waits on its socket.
///
/// This command crosses from web content into an OS action, so it accepts only
/// ordinary web links plus the two user-agent schemes the console may render.
/// In particular, a compromised page cannot ask the app to open `file:` or a
/// custom executable protocol.
pub fn open_external<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    raw: &str,
) -> Result<(), String> {
    let url = allowed_external_url(raw)
        .ok_or_else(|| format!("'{raw}' is not an allowed external URL"))?;
    let app = app.clone();
    std::thread::spawn(move || {
        if matches!(url.scheme(), "http" | "https") && open_in_cluster_chrome(url.as_str()) {
            log::info!("external link -> ava-browser new tab");
            return;
        }
        log::info!("external link -> system browser");
        if let Err(err) = app.opener().open_url(url.as_str(), None::<&str>) {
            log::error!("failed to open {url} in the system browser: {err}");
        }
    });
    Ok(())
}

fn allowed_external_url(raw: &str) -> Option<Url> {
    let url = Url::parse(raw).ok()?;
    matches!(url.scheme(), "http" | "https" | "mailto" | "tel").then_some(url)
}

/// Ask the local browser-MCP daemon for a new tab. `true` when it accepted.
#[cfg(all(unix, not(target_os = "android"), not(target_os = "ios")))]
fn open_in_cluster_chrome(url: &str) -> bool {
    use std::io::{BufRead, BufReader, Write};
    use std::os::unix::net::UnixStream;
    use std::time::Duration;

    let Some(socket) = browser_mcp_socket() else {
        return false;
    };
    let Ok(mut stream) = UnixStream::connect(&socket) else {
        return false;
    };
    // The daemon is local and single-purpose; a hung one must not hold the
    // click hostage, so both directions are bounded.
    let timeout = Some(Duration::from_secs(4));
    if stream.set_read_timeout(timeout).is_err() || stream.set_write_timeout(timeout).is_err() {
        return false;
    }
    // browser-MCP line protocol, see services/browser/mcp_daemon.py.
    let request = serde_json::json!({
        "id": 1,
        "method": "call_tool",
        "tool": "new_page",
        "args": { "url": url },
    });
    if writeln!(stream, "{request}").is_err() || stream.flush().is_err() {
        return false;
    }
    let mut line = String::new();
    if BufReader::new(&stream).read_line(&mut line).is_err() {
        return false;
    }
    serde_json::from_str::<serde_json::Value>(&line)
        .ok()
        .and_then(|v| v.get("ok").and_then(serde_json::Value::as_bool))
        .unwrap_or(false)
}

/// Windows and mobile have no cluster Chrome to hand a tab to.
#[cfg(not(all(unix, not(target_os = "android"), not(target_os = "ios"))))]
fn open_in_cluster_chrome(_url: &str) -> bool {
    false
}

/// Locate `$AVA_HOME/run/chrome-mcp.<cdp_port>.sock` (the port varies with the
/// cluster's port block, hence the scan rather than a fixed name).
#[cfg(all(unix, not(target_os = "android"), not(target_os = "ios")))]
fn browser_mcp_socket() -> Option<std::path::PathBuf> {
    let home = match std::env::var("AVA_HOME") {
        Ok(value) if !value.trim().is_empty() => std::path::PathBuf::from(value),
        _ => std::path::PathBuf::from(std::env::var("HOME").ok()?).join(".ava"),
    };
    let run_dir = home.join("run");
    for entry in std::fs::read_dir(run_dir).ok()?.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if let Some(port) = name
            .strip_prefix("chrome-mcp.")
            .and_then(|rest| rest.strip_suffix(".sock"))
        {
            if port.chars().all(|c| c.is_ascii_digit()) && !port.is_empty() {
                return Some(entry.path());
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::allowed_external_url;

    #[test]
    fn external_links_are_confined_to_user_agent_schemes() {
        for allowed in [
            "https://example.com/path",
            "http://box.local:3000/",
            "mailto:operator@example.com",
            "tel:+12025550123",
        ] {
            assert!(allowed_external_url(allowed).is_some(), "{allowed}");
        }
        for rejected in [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "ava-exec://command",
            "not a URL",
        ] {
            assert!(allowed_external_url(rejected).is_none(), "{rejected}");
        }
    }
}
