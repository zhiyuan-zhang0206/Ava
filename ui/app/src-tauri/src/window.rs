//! The main window: what it loads, what it may navigate to, and what happens
//! when the gate is not there.
//!
//! The shell owns exactly one window. It is rebuilt (rather than navigated)
//! whenever the resolved endpoints change, because the injected prelude carries
//! those endpoints into the page and a stale prelude is worse than a reload.

use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

use tauri::{AppHandle, Manager, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

use crate::commands::ShellConfig;
use crate::state::ShellState;
use crate::urls::Endpoints;
use crate::{external, urls};

pub const MAIN_WINDOW: &str = "main";

/// Retry budget for the entry URL. The gate is briefly unavailable during a
/// cluster update, so the shell waits it out instead of showing a dead window —
/// 10 attempts, 3 s apart.
const RETRY_ATTEMPTS: u32 = 10;
const RETRY_INTERVAL: Duration = Duration::from_secs(3);
/// Per-attempt reachability probe budget. Short: this is a TCP connect on the
/// local network, and a slow probe just delays the retry that follows it.
const PROBE_TIMEOUT: Duration = Duration::from_secs(2);

/// Bundled screens, addressed by fragment on the shell's own `index.html`.
const SETUP_PAGE: &str = "index.html#setup";
const CONNECTING_PAGE: &str = "index.html#connecting";
const UNREACHABLE_PAGE: &str = "index.html#unreachable";

/// Build (or rebuild) the main window for the current settings and start the
/// reachability watchdog.
///
/// With no server configured this opens the onboarding page instead; with one
/// configured it opens the gate directly, so the happy path costs no extra
/// round trip.
pub fn open_entry(app: &AppHandle) {
    if let Some(existing) = app.get_webview_window(MAIN_WINDOW) {
        // destroy(), not close(): close() goes through the CloseRequested
        // handler, which on desktop means "hide to tray" — the opposite of what
        // a rebuild wants.
        let _ = existing.destroy();
    }

    let state = app.state::<ShellState>();
    let endpoints = state.endpoints();
    if let Some(endpoints) = &endpoints {
        grant_remote_ipc(
            app,
            &endpoints.entry,
            cfg!(desktop) && state.settings().auto_login,
        );
    }

    let target = match &endpoints {
        Some(endpoints) => WebviewUrl::External(endpoints.entry.clone()),
        None => WebviewUrl::App(SETUP_PAGE.into()),
    };

    let window = match build(app, target, endpoints.clone()) {
        Ok(window) => window,
        Err(err) => {
            log::error!("could not create the main window: {err}");
            return;
        }
    };

    #[cfg(desktop)]
    crate::desktop::attach_window_behavior(&window);

    if let Some(endpoints) = endpoints {
        watch_entry(window, endpoints.entry);
    }
}

/// Create the window with the platform's script set attached.
fn build(
    app: &AppHandle,
    target: WebviewUrl,
    endpoints: Option<Endpoints>,
) -> tauri::Result<WebviewWindow> {
    let allowed = endpoints;
    let handle = app.clone();

    #[allow(unused_mut)]
    let mut builder = WebviewWindowBuilder::new(app, MAIN_WINDOW, target)
        .title("Ava")
        .inner_size(1280.0, 840.0)
        .min_inner_size(960.0, 640.0)
        .background_color(tauri::webview::Color(0x0e, 0x0e, 0x12, 0xff))
        .initialization_script(prelude(app))
        .initialization_script(include_str!("../scripts/nav-guard.js"))
        // Same-window navigation stays on the gate/gateway hosts; anything
        // else is cancelled here and handed to the external opener. This is
        // the native guard — it holds even if the injected JS never installs.
        .on_navigation(move |url| {
            if urls::is_allowed_nav(url, allowed.as_ref()) {
                return true;
            }
            let _ = external::open_external(&handle, url.as_str());
            false
        });

    #[cfg(desktop)]
    {
        if app.state::<ShellState>().settings().auto_login {
            builder = builder.initialization_script(include_str!("../scripts/auto-login.js"));
        }
    }

    #[cfg(target_os = "android")]
    {
        builder = builder
            .initialization_script(include_str!("../scripts/settings-shortcut.js"))
            .initialization_script(include_str!("../scripts/notify-bridge.js"))
            .initialization_script(include_str!("../scripts/update-check.js"));
    }

    builder.build()
}

/// `window.__AVA_SHELL__` — the resolved configuration the injected scripts
/// read. Serialized rather than templated so a URL can never break out of the
/// string it sits in.
fn prelude(app: &AppHandle) -> String {
    let state = app.state::<ShellState>();
    let config = ShellConfig::build(app, &state);
    let json = serde_json::to_string(&config).unwrap_or_else(|_| "null".to_string());
    format!("window.__AVA_SHELL__ = {json};")
}

/// Let the console's own origin — and only it — reach the shell commands the
/// injected scripts need.
///
/// The gate address is user configuration, so this capability cannot be a
/// static file; it is added at runtime for the origin actually loaded. The
/// grant is narrow on purpose: the secret-reading command is in it, so widening
/// the origin would hand a hostile page the cluster password.
fn grant_remote_ipc(app: &AppHandle, entry: &Url, allow_cluster_secret: bool) {
    use tauri::ipc::CapabilityBuilder;

    let origin = entry.origin().ascii_serialization();
    let mut capability = CapabilityBuilder::new(capability_identifier(&origin))
        .remote(format!("{origin}/*"))
        .local(false)
        .window(MAIN_WINDOW);
    for permission in REMOTE_PERMISSIONS {
        capability = capability.permission(*permission);
    }
    if allow_cluster_secret {
        capability = capability.permission(CONDITIONAL_SECRET_PERMISSION);
    }
    if let Err(err) = app.add_capability(capability) {
        log::error!("could not grant IPC access to {origin}: {err}");
    }
}

/// What the console's origin is allowed to call. Strictly what the injected
/// scripts need — the shell's settings commands are deliberately absent, so a
/// page cannot re-point the shell at a server of its choosing.
pub const REMOTE_PERMISSIONS: &[&str] = &[
    "allow-shell-open-external",
    "allow-shell-open-settings",
    "allow-shell-notify",
];

/// Only a desktop window whose persisted settings enable auto-login receives
/// this grant. The command stays in the static handler but is unreachable from
/// every other web origin and mode.
pub const CONDITIONAL_SECRET_PERMISSION: &str = "allow-shell-cluster-secret";

fn capability_identifier(origin: &str) -> String {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};

    let mut hasher = DefaultHasher::new();
    origin.hash(&mut hasher);
    format!("remote-console-{:x}", hasher.finish())
}

/// Navigate the existing window to the shell-owned settings page.
pub fn open_settings(app: &AppHandle) {
    match app.get_webview_window(MAIN_WINDOW) {
        Some(window) => navigate_to_page(&window, SETUP_PAGE),
        None => open_entry(app),
    }
}

/// Watch the entry URL and swap the window between the console and the bundled
/// status screens.
///
/// The first probe runs against a window that is already loading the gate: if
/// the gate answers, nothing happens at all and the user never sees a shell
/// screen. Only a failure escalates to "Connecting…", and only an exhausted
/// budget to "Cannot reach Ava".
fn watch_entry(window: WebviewWindow, entry: Url) {
    std::thread::spawn(move || {
        for attempt in 0..RETRY_ATTEMPTS {
            if reachable(&entry) {
                // Recovered after a visible failure — put the console back.
                if attempt > 0 {
                    let _ = window.navigate(entry.clone());
                }
                return;
            }
            if attempt == 0 {
                navigate_to_page(&window, CONNECTING_PAGE);
            }
            std::thread::sleep(RETRY_INTERVAL);
        }
        log::error!("{entry} did not answer after {RETRY_ATTEMPTS} attempts");
        navigate_to_page(&window, UNREACHABLE_PAGE);
    });
}

/// TCP-connect probe. The failure this retry loop exists for is "the gate
/// process is not listening yet", which a connect answers directly — no HTTP
/// client, no TLS, and no opinion about what the server replies.
fn reachable(url: &Url) -> bool {
    let Some(host) = url.host_str() else {
        return false;
    };
    let Some(port) = url.port_or_known_default() else {
        return false;
    };
    let Ok(addresses) = (host, port).to_socket_addrs() else {
        return false;
    };
    addresses
        .into_iter()
        .any(|address| TcpStream::connect_timeout(&address, PROBE_TIMEOUT).is_ok())
}

/// Navigate to one of the bundled screens, whose origin differs per platform.
fn navigate_to_page(window: &WebviewWindow, page: &str) {
    let base = if cfg!(windows) || cfg!(target_os = "android") {
        "http://tauri.localhost/"
    } else {
        "tauri://localhost/"
    };
    match Url::parse(&format!("{base}{page}")) {
        Ok(url) => {
            let _ = window.navigate(url);
        }
        Err(err) => log::error!("could not build the {page} URL: {err}"),
    }
}

#[cfg(test)]
mod tests {
    use super::capability_identifier;

    #[test]
    fn runtime_capability_identifiers_are_valid_and_origin_specific() {
        let first = capability_identifier("http://localhost:3000");
        let second = capability_identifier("https://ava.example.com");
        assert!(first.starts_with("remote-console-"));
        assert!(first.chars().all(|c| c.is_ascii_alphanumeric() || c == '-'));
        assert_ne!(first, second);
    }
}
