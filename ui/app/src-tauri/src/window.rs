//! The main window: what it loads, what it may navigate to, and what happens
//! when the gate is not there.
//!
//! The app owns exactly one window. Desktop rebuilds it whenever the resolved
//! endpoints change; Android refreshes the injected prelude and navigates the
//! existing webview because rebuilding wedges wry's Android IPC pipe.

use std::time::{Duration, Instant};

use reqwest::blocking::Client;
use reqwest::redirect::Policy;
use reqwest::StatusCode;
use tauri::{AppHandle, Manager, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

use crate::commands::AppConfig;
use crate::state::AppState;
use crate::urls::Endpoints;
use crate::{external, urls};

pub const MAIN_WINDOW: &str = "main";

/// Overall entry probe budget. A failed connection must settle to a readable
/// recovery screen within the same 30-second budget the app UI displays.
const RETRY_BUDGET: Duration = Duration::from_secs(30);
const RETRY_INTERVAL: Duration = Duration::from_secs(3);
/// HTTP answers matter here: TCP can accept then leave a webview hanging.
const PROBE_TIMEOUT: Duration = Duration::from_secs(4);

/// Bundled screens, addressed by fragment on the app's own `index.html`.
const SETUP_PAGE: &str = "index.html#setup";
const CONNECTING_PAGE: &str = "index.html#connecting";

/// Open the main window for the current settings and start the reachability
/// watchdog.
///
/// With no server configured this opens the onboarding page instead; with one
/// configured it opens the gate directly, so the happy path costs no extra
/// round trip.
pub fn open_entry(app: &AppHandle) {
    let state = app.state::<AppState>();
    let endpoints = state.endpoints();

    #[cfg(target_os = "android")]
    if let (Some(window), Some(endpoints)) =
        (app.get_webview_window(MAIN_WINDOW), endpoints.clone())
    {
        grant_remote_ipc(app, &endpoints.entry);
        let navigation_window = window.clone();
        let entry_url = endpoints.entry.clone();
        if let Err(err) = window.eval_with_callback(prelude(app), move |_| {
            if let Err(err) = navigation_window.navigate(entry_url.clone()) {
                log::error!("could not navigate the Android window to {entry_url}: {err}");
            }
        }) {
            log::error!("could not refresh the Android window prelude: {err}");
        }
        attach_entry(app, window, endpoints);
        return;
    }

    if let Some(existing) = app.get_webview_window(MAIN_WINDOW) {
        // destroy(), not close(): close() goes through the CloseRequested
        // handler, which on desktop means "hide to tray" — the opposite of what
        // a rebuild wants.
        let _ = existing.destroy();
    }

    if let Some(endpoints) = &endpoints {
        grant_remote_ipc(app, &endpoints.entry);
    }

    let target = match &endpoints {
        Some(endpoints) => WebviewUrl::External(endpoints.entry.clone()),
        None => WebviewUrl::App(SETUP_PAGE.into()),
    };

    let window = match build(app, target) {
        Ok(window) => window,
        Err(err) => {
            log::error!("could not create the main window: {err}");
            return;
        }
    };

    #[cfg(desktop)]
    crate::desktop::attach_window_behavior(&window);

    if let Some(endpoints) = endpoints {
        attach_entry(app, window, endpoints);
    }
}

/// Start platform login and readiness work after a window starts or its Android
/// prelude has refreshed and it has navigated to the current entry URL.
fn attach_entry(app: &AppHandle, window: WebviewWindow, endpoints: Endpoints) {
    #[cfg(desktop)]
    if app.state::<AppState>().settings().auto_login {
        if let Some(secret) = crate::autologin::cluster_secret() {
            crate::autologin::start(window.clone(), endpoints.clone(), secret);
        }
    }
    #[cfg(target_os = "android")]
    if !app.state::<AppState>().take_skip_next_android_autologin() {
        let handle = app.clone();
        let login_window = window.clone();
        let login_endpoints = endpoints.clone();
        tauri::async_runtime::spawn(async move {
            if let Some(secret) = crate::android::load_stored_secret(&handle).await {
                crate::autologin::start(handle, login_window, login_endpoints, secret);
            }
        });
    }
    watch_entry(window, endpoints.entry);
}

/// Create the window with the platform's script set attached.
fn build(app: &AppHandle, target: WebviewUrl) -> tauri::Result<WebviewWindow> {
    let navigation_handle = app.clone();
    let page_load_handle = app.clone();

    #[allow(unused_mut)]
    let mut builder = WebviewWindowBuilder::new(app, MAIN_WINDOW, target)
        .title("Ava")
        .inner_size(1280.0, 840.0)
        .min_inner_size(960.0, 640.0)
        .background_color(tauri::webview::Color(0x0e, 0x0e, 0x12, 0xff))
        .initialization_script(prelude(app))
        .initialization_script(include_str!("../scripts/nav-guard.js"))
        // Same-window navigation resolves its allowlist from live settings, so
        // Android's in-place post-save navigation reaches the new gate. Other
        // URLs are cancelled here and handed to the external opener.
        .on_navigation(move |url| {
            let allowed = navigation_handle.state::<AppState>().endpoints();
            if urls::is_allowed_nav(url, allowed.as_ref()) {
                return true;
            }
            let _ = external::open_external(&navigation_handle, url.as_str());
            false
        })
        .on_page_load(move |window, _payload| {
            let script = format!(
                "{}window.dispatchEvent(new Event('ava-app-config'));",
                prelude(&page_load_handle),
            );
            if let Err(err) = window.eval(&script) {
                log::error!("could not refresh the app config after page load: {err}");
            }
        });

    #[cfg(target_os = "android")]
    {
        builder = builder
            .initialization_script(include_str!("../scripts/settings-shortcut.js"))
            .initialization_script(include_str!("../scripts/notify-bridge.js"))
            .initialization_script(include_str!("../scripts/update-check.js"));
    }

    builder.build()
}

/// `window.__AVA_APP__` — the resolved configuration the injected scripts
/// read. Serialized rather than templated so a URL can never break out of the
/// string it sits in.
fn prelude(app: &AppHandle) -> String {
    let state = app.state::<AppState>();
    let config = AppConfig::build(app, &state);
    let json = serde_json::to_string(&config).unwrap_or_else(|_| "null".to_string());
    format!("window.__AVA_APP__ = {json};")
}

/// Let the console's own origin — and only it — reach the app commands the
/// injected scripts need.
///
/// The gate address is user configuration, so this capability cannot be a
/// static file; it is added at runtime for the origin actually loaded. The
/// grant is narrow on purpose: the console can open external links, settings,
/// and Android notifications, but it cannot read local credentials or persist
/// a new server address.
fn grant_remote_ipc(app: &AppHandle, entry: &Url) {
    use tauri::ipc::CapabilityBuilder;

    let origin = entry.origin().ascii_serialization();
    let mut capability = CapabilityBuilder::new(capability_identifier(&origin))
        .remote(format!("{origin}/*"))
        .local(false)
        .window(MAIN_WINDOW);
    for permission in REMOTE_PERMISSIONS {
        capability = capability.permission(*permission);
    }
    if let Err(err) = app.add_capability(capability) {
        log::error!("could not grant IPC access to {origin}: {err}");
    }
}

/// What the console's origin is allowed to call. Strictly what the injected
/// scripts need — the app's settings commands are deliberately absent, so a
/// page cannot re-point the app at a server of its choosing.
pub const REMOTE_PERMISSIONS: &[&str] = &[
    "allow-app-open-external",
    "allow-app-open-settings",
    "allow-app-notify",
    "allow-app-take-pending-click",
];

fn capability_identifier(origin: &str) -> String {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};

    let mut hasher = DefaultHasher::new();
    origin.hash(&mut hasher);
    format!("remote-console-{:x}", hasher.finish())
}

/// Navigate the existing window to the app-owned settings page.
pub fn open_settings(app: &AppHandle) {
    match app.get_webview_window(MAIN_WINDOW) {
        Some(window) => navigate_to_page(&window, SETUP_PAGE),
        None => open_entry(app),
    }
}

/// The user-facing category for an exhausted HTTP probe budget.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum EntryFailure {
    Unreachable,
    Http,
    UpdateWindow,
}

impl EntryFailure {
    const fn query_value(self) -> &'static str {
        match self {
            Self::Unreachable => "unreachable",
            Self::Http => "http",
            Self::UpdateWindow => "update-window",
        }
    }

    /// Preserve the most actionable observation across retry attempts.
    const fn combine(self, next: Self) -> Self {
        match (self, next) {
            (_, Self::UpdateWindow) | (Self::UpdateWindow, _) => Self::UpdateWindow,
            (_, Self::Http) | (Self::Http, _) => Self::Http,
            _ => Self::Unreachable,
        }
    }
}

/// Watch the entry URL and swap the window between the console and the bundled
/// status screens.
///
/// The first probe runs against a window that is already loading the gate: if
/// the gate answers, nothing happens at all and the user never sees an app
/// screen. Only a failure escalates to "Connecting…"; the 30-second budget
/// then ends at a classified recovery screen.
fn watch_entry(window: WebviewWindow, entry: Url) {
    let client = match Client::builder()
        .timeout(PROBE_TIMEOUT)
        .redirect(Policy::none())
        .build()
    {
        Ok(client) => client,
        Err(err) => {
            log::error!("could not build the entry probe client: {err}");
            navigate_to_page(&window, &unreachable_page(EntryFailure::Http));
            return;
        }
    };
    std::thread::spawn(move || {
        let deadline = Instant::now() + RETRY_BUDGET;
        let mut attempt = 0;
        let mut failure = EntryFailure::Unreachable;
        loop {
            match probe_entry(&client, &entry) {
                Ok(()) => {
                    // Recovered after a visible failure — put the console back.
                    if attempt > 0 {
                        let _ = window.navigate(entry.clone());
                    }
                    return;
                }
                Err(next) => failure = failure.combine(next),
            }
            if attempt == 0 {
                navigate_to_page(&window, CONNECTING_PAGE);
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                log::error!(
                    "{entry} did not answer within {} seconds",
                    RETRY_BUDGET.as_secs()
                );
                navigate_to_page(&window, &unreachable_page(failure));
                return;
            }
            std::thread::sleep(RETRY_INTERVAL.min(remaining));
            attempt += 1;
        }
    });
}

/// HTTP GET probe for the console root. A listening TCP socket is insufficient:
/// it can accept a webview connection then hang forever. The console's normal
/// signed-out answers (401/403) are still healthy HTTP responses.
fn probe_entry(client: &Client, url: &Url) -> Result<(), EntryFailure> {
    let response = client
        .get(url.as_str())
        .send()
        .map_err(|_| EntryFailure::Unreachable)?;
    classify_probe_status(response.status())
}

fn classify_probe_status(status: StatusCode) -> Result<(), EntryFailure> {
    if status.is_success()
        || status.is_redirection()
        || matches!(status, StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN)
    {
        return Ok(());
    }
    if status == StatusCode::SERVICE_UNAVAILABLE {
        return Err(EntryFailure::UpdateWindow);
    }
    Err(EntryFailure::Http)
}

fn unreachable_page(failure: EntryFailure) -> String {
    format!("index.html?reason={}#unreachable", failure.query_value())
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
    use super::{capability_identifier, classify_probe_status, unreachable_page, EntryFailure};
    use reqwest::StatusCode;

    #[test]
    fn runtime_capability_identifiers_are_valid_and_origin_specific() {
        let first = capability_identifier("http://localhost:3000");
        let second = capability_identifier("https://ava.example.com");
        assert!(first.starts_with("remote-console-"));
        assert!(first.chars().all(|c| c.is_ascii_alphanumeric() || c == '-'));
        assert_ne!(first, second);
    }

    #[test]
    fn http_probe_accepts_console_and_login_responses() {
        for status in [
            StatusCode::OK,
            StatusCode::FOUND,
            StatusCode::UNAUTHORIZED,
            StatusCode::FORBIDDEN,
        ] {
            assert_eq!(classify_probe_status(status), Ok(()));
        }
        assert_eq!(
            classify_probe_status(StatusCode::INTERNAL_SERVER_ERROR),
            Err(EntryFailure::Http)
        );
        assert_eq!(
            classify_probe_status(StatusCode::SERVICE_UNAVAILABLE),
            Err(EntryFailure::UpdateWindow)
        );
    }

    #[test]
    fn failure_page_carries_a_machine_readable_reason() {
        assert_eq!(
            unreachable_page(EntryFailure::Unreachable),
            "index.html?reason=unreachable#unreachable"
        );
    }
}
