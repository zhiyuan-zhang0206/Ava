//! Native login — exchange a cluster secret for a short-lived session cookie
//! without exposing the long-lived secret to remote web code.
//!
//! Desktop reads its local cluster secret from `$AVA_HOME/.env`; Android passes
//! a secret held by its Keystore plugin. In both cases Rust sends the password
//! directly to the gateway, installs only the returned HTTP-only cookie in the
//! webview store, and loads the console after a successful exchange.

#[cfg(desktop)]
use std::env;
#[cfg(desktop)]
use std::path::PathBuf;
use std::time::Duration;

use reqwest::blocking::Client;
use reqwest::header::SET_COOKIE;
use reqwest::redirect::Policy;
use tauri::webview::{cookie, Cookie};
#[cfg(target_os = "android")]
use tauri::AppHandle;
use tauri::{Url, WebviewWindow};

use crate::urls::Endpoints;

/// Env key carrying the desktop cluster secret in `$AVA_HOME/.env`.
#[cfg(desktop)]
const SECRET_KEY: &str = "AVA_CLUSTER_SECRET";

/// Keep a stalled gateway from pinning the native login helper indefinitely.
const LOGIN_TIMEOUT: Duration = Duration::from_secs(10);

/// A login failure that is safe to report without exposing the secret.
#[derive(Debug)]
pub enum LoginError {
    /// The gateway received the secret and explicitly rejected it.
    Rejected,
    /// Transport, protocol, or cookie-installation failure.
    Unavailable(String),
}

#[cfg(target_os = "android")]
impl LoginError {
    /// User-safe command error text; no variant carries the submitted secret.
    pub fn message(&self) -> String {
        match self {
            Self::Rejected => "AUTH_FAILED: cluster secret was rejected".to_string(),
            Self::Unavailable(message) => message.clone(),
        }
    }
}

/// Start one native login attempt and reload the console if it succeeds.
///
/// Startup callers intentionally do not retry failures: a rejected stored
/// secret must fall back to the regular console login instead of risking the
/// gateway's brute-force protections.
#[cfg(desktop)]
pub fn start(window: WebviewWindow, endpoints: Endpoints, secret: String) {
    std::thread::spawn(move || match login(&window, &endpoints, &secret) {
        Ok(()) => {
            if let Err(err) = window.navigate(endpoints.entry) {
                log::error!("could not reload the console after auto-login: {err}");
            }
        }
        Err(LoginError::Rejected) => {
            log::warn!("stored auto-login secret was rejected; showing the console login");
        }
        Err(LoginError::Unavailable(err)) => log::warn!("native auto-login unavailable: {err}"),
    });
}

/// Exchange `secret` for a session cookie and install it into `window`.
///
/// This intentionally leaves navigation to the caller: onboarding needs to
/// persist a Keystore secret only after the cookie exchange has succeeded,
/// while startup reloads the already-created console window afterwards.
#[cfg(desktop)]
pub fn login(
    window: &WebviewWindow,
    endpoints: &Endpoints,
    secret: &str,
) -> Result<(), LoginError> {
    let cookie = login_cookie(&endpoints.gateway, secret)?;
    window
        .set_cookie(cookie)
        .map_err(|err| LoginError::Unavailable(format!("could not install session cookie: {err}")))
}

/// Start one Android native login attempt through the platform cookie store.
#[cfg(target_os = "android")]
pub fn start(app: AppHandle, window: WebviewWindow, endpoints: Endpoints, secret: String) {
    tauri::async_runtime::spawn(async move {
        match login(&app, &endpoints, &secret).await {
            Ok(()) => {
                if let Err(err) = window.navigate(endpoints.entry) {
                    log::error!("could not reload the console after auto-login: {err}");
                }
            }
            Err(LoginError::Rejected) => {
                log::warn!("stored auto-login secret was rejected; showing the console login");
            }
            Err(LoginError::Unavailable(err)) => {
                log::warn!("native auto-login unavailable: {err}");
            }
        }
    });
}

/// Exchange `secret` for a session cookie and install it through Android's
/// `CookieManager`, which is the WebView store the console actually reads.
#[cfg(target_os = "android")]
pub async fn login(app: &AppHandle, endpoints: &Endpoints, secret: &str) -> Result<(), LoginError> {
    let gateway = endpoints.gateway.clone();
    let secret = secret.to_string();
    let cookie = tauri::async_runtime::spawn_blocking(move || login_cookie(&gateway, &secret))
        .await
        .map_err(|err| LoginError::Unavailable(format!("native login task failed: {err}")))??;
    crate::android::install_session_cookie_async(
        app,
        endpoints.gateway.as_str(),
        cookie.to_string(),
    )
    .await
    .map_err(LoginError::Unavailable)
}

/// Exchange the secret for a cookie. The response body is intentionally
/// ignored; the status and cookie header are the complete login contract.
fn login_cookie(gateway: &Url, secret: &str) -> Result<Cookie<'static>, LoginError> {
    let login_url = gateway
        .join("/api/auth/login")
        .map_err(|err| LoginError::Unavailable(format!("invalid gateway login URL: {err}")))?;
    let client = Client::builder()
        .timeout(LOGIN_TIMEOUT)
        // Never follow a redirect with the cluster secret in the POST body.
        // The configured gateway endpoint itself must answer the login.
        .redirect(Policy::none())
        .build()
        .map_err(|err| LoginError::Unavailable(format!("could not build login client: {err}")))?;
    let response = client
        .post(login_url.as_str())
        .json(&serde_json::json!({ "password": secret }))
        .send()
        .map_err(|err| LoginError::Unavailable(format!("gateway login request failed: {err}")))?;
    if matches!(response.status().as_u16(), 401 | 403) {
        return Err(LoginError::Rejected);
    }
    if !response.status().is_success() {
        return Err(LoginError::Unavailable(format!(
            "gateway rejected login ({})",
            response.status()
        )));
    }
    let header = response
        .headers()
        .get(SET_COOKIE)
        .ok_or_else(|| {
            LoginError::Unavailable("gateway login returned no session cookie".to_string())
        })?
        .to_str()
        .map_err(|_| {
            LoginError::Unavailable("gateway login returned a non-text cookie".to_string())
        })?;
    cookie_for_gateway(header, gateway)
}

/// Attach the response cookie to the gateway's exact host before handing it to
/// the native webview store. WebKit and WebView2 distinguish an exact domain
/// (`gateway.example`) from a subdomain-matching one (`.gateway.example`); the
/// URL host can never carry that leading dot. Cookie domains contain no port.
fn cookie_for_gateway(header: &str, gateway: &Url) -> Result<Cookie<'static>, LoginError> {
    let host = gateway
        .host_str()
        .ok_or_else(|| LoginError::Unavailable("gateway URL has no host".to_string()))?;
    let parsed = cookie::Cookie::parse(header.to_string()).map_err(|err| {
        LoginError::Unavailable(format!("gateway returned an invalid cookie: {err}"))
    })?;
    let mut builder =
        cookie::Cookie::build((parsed.name().to_string(), parsed.value().to_string()))
            .domain(host.to_string())
            .path(parsed.path().unwrap_or("/").to_string());
    if let Some(value) = parsed.http_only() {
        builder = builder.http_only(value);
    }
    if let Some(value) = parsed.secure() {
        builder = builder.secure(value);
    }
    if let Some(value) = parsed.same_site() {
        builder = builder.same_site(value);
    }
    if let Some(value) = parsed.max_age() {
        builder = builder.max_age(value);
    }
    Ok(builder.build().into_owned())
}

/// The desktop `.env` path: `$AVA_HOME/.env`, falling back to `~/.ava/.env`.
#[cfg(desktop)]
fn env_file() -> Option<PathBuf> {
    let home = match env::var("AVA_HOME") {
        Ok(value) if !value.trim().is_empty() => PathBuf::from(value),
        _ => dirs_home()?.join(".ava"),
    };
    Some(home.join(".env"))
}

/// Home directory without pulling in a crate for it: `$HOME` on unix,
/// `%USERPROFILE%` on Windows.
#[cfg(desktop)]
fn dirs_home() -> Option<PathBuf> {
    #[cfg(windows)]
    let raw = env::var("USERPROFILE").ok();
    #[cfg(not(windows))]
    let raw = env::var("HOME").ok();
    raw.filter(|value| !value.trim().is_empty())
        .map(PathBuf::from)
}

/// Read the desktop cluster secret from `$AVA_HOME/.env`.
///
/// `None` means "no local cluster here" and is normal for a frontend-only
/// desktop machine. The value is never logged.
#[cfg(desktop)]
pub fn cluster_secret() -> Option<String> {
    let path = env_file()?;
    let text = std::fs::read_to_string(path).ok()?;
    parse_secret(&text)
}

/// Extract the secret from `.env` text. Tolerates surrounding whitespace and
/// `export ` prefixes; an empty assignment is the no-auth cluster mode.
#[cfg(desktop)]
fn parse_secret(text: &str) -> Option<String> {
    for line in text.lines() {
        let line = line.trim();
        let line = line.strip_prefix("export ").unwrap_or(line);
        let Some(value) = line.strip_prefix(SECRET_KEY) else {
            continue;
        };
        let Some(value) = value.strip_prefix('=') else {
            continue;
        };
        let value = value.trim().trim_matches('"').trim_matches('\'');
        if !value.is_empty() {
            return Some(value.to_string());
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::cookie_for_gateway;
    #[cfg(desktop)]
    use super::parse_secret;
    use tauri::webview::cookie::SameSite;
    use tauri::Url;

    #[cfg(desktop)]
    #[test]
    fn reads_the_secret_line() {
        assert_eq!(
            parse_secret("AVA_DB_URL=postgresql://x\nAVA_CLUSTER_SECRET=s3cret\n").as_deref(),
            Some("s3cret")
        );
    }

    #[cfg(desktop)]
    #[test]
    fn tolerates_export_prefix_quotes_and_padding() {
        assert_eq!(
            parse_secret("  export AVA_CLUSTER_SECRET=\"quoted\"  \n").as_deref(),
            Some("quoted")
        );
    }

    #[cfg(desktop)]
    #[test]
    fn empty_assignment_is_the_no_auth_cluster_and_yields_nothing() {
        assert!(parse_secret("AVA_CLUSTER_SECRET=\n").is_none());
        assert!(parse_secret("AVA_CLUSTER_SECRET=   \n").is_none());
    }

    #[cfg(desktop)]
    #[test]
    fn a_similarly_named_key_is_not_the_secret() {
        assert!(parse_secret("AVA_CLUSTER_SECRET_FILE=/tmp/x\n").is_none());
    }

    #[cfg(desktop)]
    #[test]
    fn absent_key_yields_nothing() {
        assert!(parse_secret("AVA_DB_URL=postgresql://x\n").is_none());
    }

    #[test]
    fn gateway_cookie_keeps_security_flags_and_gets_the_gateway_domain() {
        let gateway = Url::parse("https://ava.example.com:8000/").unwrap();
        let cookie = cookie_for_gateway(
            "ava_session=token; HttpOnly; SameSite=Lax; Secure; Path=/; Max-Age=604800",
            &gateway,
        )
        .unwrap();
        assert_eq!(cookie.name(), "ava_session");
        assert_eq!(cookie.value(), "token");
        assert_eq!(cookie.domain(), Some("ava.example.com"));
        assert!(!cookie.domain().unwrap().starts_with('.'));
        assert_eq!(cookie.path(), Some("/"));
        assert_eq!(cookie.http_only(), Some(true));
        assert_eq!(cookie.secure(), Some(true));
        assert_eq!(cookie.same_site(), Some(SameSite::Lax));
        assert_eq!(
            cookie.max_age().map(|age| age.whole_seconds()),
            Some(604_800)
        );
    }
}
