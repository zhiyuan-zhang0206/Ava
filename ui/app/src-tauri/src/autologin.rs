//! Desktop auto-login — exchange the local cluster secret for a short-lived
//! session cookie without exposing the long-lived secret to remote web code.
//!
//! The request runs in native Rust. Only the gateway's `Set-Cookie` result is
//! installed in the webview store, then the console is reloaded. Consequently
//! a compromised console origin can reach only the same seven-day HTTP-only
//! session it already runs inside; it cannot invoke an IPC command to extract
//! the cluster-wide credential.
//!
//! A machine with no local cluster (`.env` absent) simply gets the login page.

use std::env;
use std::path::PathBuf;
use std::time::Duration;

use reqwest::blocking::Client;
use reqwest::header::SET_COOKIE;
use reqwest::redirect::Policy;
use tauri::webview::{cookie, Cookie};
use tauri::{Url, WebviewWindow};

use crate::urls::Endpoints;

/// Env key carrying the cluster secret in `$AVA_HOME/.env`.
const SECRET_KEY: &str = "AVA_CLUSTER_SECRET";

/// Keep a stalled gateway from pinning the login helper thread indefinitely.
const LOGIN_TIMEOUT: Duration = Duration::from_secs(10);

/// Start one native login attempt and reload the console if it succeeds.
///
/// Transport/auth failures deliberately fall back to the ordinary login page.
/// In particular, an invalid local secret is never retried: repeated guesses
/// would trip the gateway's brute-force lockout.
pub fn start(window: WebviewWindow, endpoints: Endpoints) {
    let Some(secret) = cluster_secret() else {
        return;
    };

    std::thread::spawn(move || match login_cookie(&endpoints.gateway, &secret) {
        Ok(cookie) => {
            if let Err(err) = window.set_cookie(cookie) {
                log::error!("could not install the auto-login session: {err}");
                return;
            }
            if let Err(err) = window.navigate(endpoints.entry) {
                log::error!("could not reload the console after auto-login: {err}");
            }
        }
        Err(err) => log::warn!("desktop auto-login unavailable: {err}"),
    });
}

/// Exchange the secret for a cookie. The response body is intentionally
/// ignored; the status and cookie header are the complete login contract.
fn login_cookie(gateway: &Url, secret: &str) -> Result<Cookie<'static>, String> {
    let login_url = gateway
        .join("/api/auth/login")
        .map_err(|err| format!("invalid gateway login URL: {err}"))?;
    let client = Client::builder()
        .timeout(LOGIN_TIMEOUT)
        // Never follow a redirect with the cluster secret in the POST body.
        // The configured gateway endpoint itself must answer the login.
        .redirect(Policy::none())
        .build()
        .map_err(|err| format!("could not build login client: {err}"))?;
    let response = client
        .post(login_url.as_str())
        .json(&serde_json::json!({ "password": secret }))
        .send()
        .map_err(|err| format!("gateway login request failed: {err}"))?;
    if !response.status().is_success() {
        return Err(format!("gateway rejected login ({})", response.status()));
    }
    let header = response
        .headers()
        .get(SET_COOKIE)
        .ok_or_else(|| "gateway login returned no session cookie".to_string())?
        .to_str()
        .map_err(|_| "gateway login returned a non-text cookie".to_string())?;
    cookie_for_gateway(header, gateway)
}

/// Attach the response cookie to the gateway's exact host before handing it to
/// the native webview store. WebKit and WebView2 distinguish an exact domain
/// (`gateway.example`) from a subdomain-matching one (`.gateway.example`); the
/// URL host can never carry that leading dot. Cookie domains contain no port.
fn cookie_for_gateway(header: &str, gateway: &Url) -> Result<Cookie<'static>, String> {
    let host = gateway
        .host_str()
        .ok_or_else(|| "gateway URL has no host".to_string())?;
    let parsed = cookie::Cookie::parse(header.to_string())
        .map_err(|err| format!("gateway returned an invalid cookie: {err}"))?;
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

/// The `.env` the local cluster writes at install time: `$AVA_HOME/.env`,
/// falling back to the default home `~/.ava/.env`.
fn env_file() -> Option<PathBuf> {
    let home = match env::var("AVA_HOME") {
        Ok(value) if !value.trim().is_empty() => PathBuf::from(value),
        _ => dirs_home()?.join(".ava"),
    };
    Some(home.join(".env"))
}

/// Home directory without pulling in a crate for it: `$HOME` on unix,
/// `%USERPROFILE%` on Windows.
fn dirs_home() -> Option<PathBuf> {
    #[cfg(windows)]
    let raw = env::var("USERPROFILE").ok();
    #[cfg(not(windows))]
    let raw = env::var("HOME").ok();
    raw.filter(|v| !v.trim().is_empty()).map(PathBuf::from)
}

/// Read `AVA_CLUSTER_SECRET` from the local cluster `.env`.
///
/// `None` means "no local cluster here" — a frontend-only machine — and is a
/// normal outcome, not an error. The value is never logged.
pub fn cluster_secret() -> Option<String> {
    let path = env_file()?;
    let text = std::fs::read_to_string(path).ok()?;
    parse_secret(&text)
}

/// Extract the secret from `.env` text. Tolerates surrounding whitespace and
/// `export ` prefixes; an empty assignment counts as absent (an empty secret is
/// the cluster's no-auth mode, where auto-login has nothing to present).
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
    use super::{cookie_for_gateway, parse_secret};
    use tauri::webview::cookie::SameSite;
    use tauri::Url;

    #[test]
    fn reads_the_secret_line() {
        assert_eq!(
            parse_secret("AVA_DB_URL=postgresql://x\nAVA_CLUSTER_SECRET=s3cret\n").as_deref(),
            Some("s3cret")
        );
    }

    #[test]
    fn tolerates_export_prefix_quotes_and_padding() {
        assert_eq!(
            parse_secret("  export AVA_CLUSTER_SECRET=\"quoted\"  \n").as_deref(),
            Some("quoted")
        );
    }

    #[test]
    fn empty_assignment_is_the_no_auth_cluster_and_yields_nothing() {
        assert!(parse_secret("AVA_CLUSTER_SECRET=\n").is_none());
        assert!(parse_secret("AVA_CLUSTER_SECRET=   \n").is_none());
    }

    #[test]
    fn a_similarly_named_key_is_not_the_secret() {
        assert!(parse_secret("AVA_CLUSTER_SECRET_FILE=/tmp/x\n").is_none());
    }

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
