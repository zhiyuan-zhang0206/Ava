//! Desktop auto-login — read the local cluster secret so a machine that owns a
//! cluster never has to type its own password.
//!
//! This module only *finds* the secret. The login itself happens in the
//! webview, from the injected `auto-login.js`: it calls `/api/auth/check`, and
//! when signed out POSTs `/api/auth/login` with the secret the way the console's
//! own login form does. The browser stack then sets and persists the cookie
//! natively, with no second cookie-jar injection path.
//!
//! A machine with no local cluster (`.env` absent) simply gets the login page.

use std::env;
use std::path::PathBuf;

/// Env key carrying the cluster secret in `$AVA_HOME/.env`.
const SECRET_KEY: &str = "AVA_CLUSTER_SECRET";

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
    use super::parse_secret;

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
}
