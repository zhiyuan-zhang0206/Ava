//! Endpoint resolution and the same-window navigation allow list.
//!
//! Two facts about the cluster shape drive everything here:
//!
//! * the gate (entry) and the gateway are the **same host on different ports**,
//!   which is exactly how the web console derives `API_BASE`; and
//! * the session cookie is host-only, so an entry page and a gateway on
//!   different hosts leave the cookie on the wrong host and login loops.
//!
//! So the gateway is derived from the entry URL unless the user pins one, and
//! same-window navigation is confined to those two exact origins.

use std::net::ToSocketAddrs;

use tauri::Url;

use crate::settings::Settings;

/// Gateway port the cluster serves its HTTP API on. Mirrors the web console's
/// `NEXT_PUBLIC_GATEWAY_PORT` default.
pub const DEFAULT_GATEWAY_PORT: u16 = 8000;

/// Port the gate listens on — what a bare host in the onboarding field means.
const GATE_PORT: u16 = 3000;

/// The primary server field names a cluster, not an arbitrary service. Custom
/// ports remain available through the advanced gateway override.
#[cfg(any(target_os = "android", test))]
pub(crate) const CUSTOM_ENTRY_PORT_ERROR: &str =
    "控制台端口为 3000，网关端口 8000 自动推导；自定义端口请走高级设置。";

/// The pair of URLs the app talks to.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Endpoints {
    /// The gate — what the main window loads.
    pub entry: Url,
    /// The gateway — what the injected scripts call for auth and SSE.
    pub gateway: Url,
}

/// Parse a user-supplied server address.
///
/// A bare host (`box.local`, `100.64.3.2`) or `host:port` is accepted and
/// completed into `http://<host>:<port or 3000>`; anything with a scheme is
/// parsed as-is. Returns `None` for input that cannot become an http(s) URL.
pub fn parse_entry(raw: &str) -> Option<Url> {
    let raw = raw.trim().trim_end_matches('/');
    if raw.is_empty() {
        return None;
    }
    let scheme_supplied = raw.contains("://");
    let candidate = if scheme_supplied {
        raw.to_string()
    } else {
        format!("http://{raw}")
    };
    let mut url = Url::parse(&candidate).ok()?;
    if !matches!(url.scheme(), "http" | "https") {
        return None;
    }
    url.host_str()?;
    // A bare host means the gate's own port, not the scheme default: someone
    // typing `box.local` into the onboarding field means the console, and the
    // console is not on port 80.
    if url.port().is_none() && !scheme_supplied {
        url.set_port(Some(GATE_PORT)).ok()?;
    }
    Some(url)
}

/// Canonicalize the primary server field before persisting it.
///
/// A cluster has one user-facing address: the console. Pasting its default
/// gateway URL therefore maps to the console, while arbitrary ports belong to
/// the explicit advanced override instead of being guessed here.
#[cfg(any(target_os = "android", test))]
pub(crate) fn normalize_entry_address(raw: &str) -> Result<String, String> {
    normalize_address(raw, GATE_PORT, true)
}

/// Canonicalize the optional advanced gateway override before persisting it.
///
/// Custom ports deliberately pass through here; existing `settings.json`
/// overrides are a supported advanced escape hatch.
#[cfg(any(target_os = "android", test))]
pub(crate) fn normalize_gateway_address(raw: &str) -> Result<String, String> {
    normalize_address(raw, DEFAULT_GATEWAY_PORT, false)
}

#[cfg(any(target_os = "android", test))]
fn normalize_address(
    raw: &str,
    target_port: u16,
    reject_custom_port: bool,
) -> Result<String, String> {
    let raw = raw.trim();
    let candidate = if raw.contains("://") {
        raw.to_string()
    } else {
        format!("http://{raw}")
    };
    let explicit_port = explicit_port(raw);
    let mut url = Url::parse(&candidate)
        .ok()
        .filter(|url| matches!(url.scheme(), "http" | "https") && url.host_str().is_some())
        .ok_or_else(|| format!("'{raw}' is not a server address"))?;

    match explicit_port.or(url.port()) {
        Some(port) if port == target_port => {}
        Some(port) if port == alternate_cluster_port(target_port) => {
            url.set_port(Some(target_port))
                .expect("HTTP(S) URLs with a host can carry a port");
        }
        Some(_) if reject_custom_port => return Err(CUSTOM_ENTRY_PORT_ERROR.to_string()),
        Some(_) => {}
        None => {
            url.set_port(Some(target_port))
                .expect("HTTP(S) URLs with a host can carry a port");
        }
    }

    // Onboarding stores origins, not deep links: the window must always load
    // the console root and derive its gateway from the same authority.
    url.set_username("")
        .expect("HTTP(S) URLs with a host can remove a username");
    url.set_password(None)
        .expect("HTTP(S) URLs with a host can remove a password");
    url.set_path("");
    url.set_query(None);
    url.set_fragment(None);
    Ok(url.to_string())
}

#[cfg(any(target_os = "android", test))]
fn alternate_cluster_port(target_port: u16) -> u16 {
    match target_port {
        GATE_PORT => DEFAULT_GATEWAY_PORT,
        DEFAULT_GATEWAY_PORT => GATE_PORT,
        _ => unreachable!("cluster address normalization only has cluster ports"),
    }
}

/// `Url` removes a scheme's default port while parsing, but an explicitly
/// entered `:80` or `:443` is still a custom port in the primary field.
#[cfg(any(target_os = "android", test))]
fn explicit_port(raw: &str) -> Option<u16> {
    let authority = raw
        .split_once("://")
        .map_or(raw, |(_, remainder)| remainder)
        .split(['/', '?', '#'])
        .next()
        .unwrap_or_default()
        .rsplit('@')
        .next()
        .unwrap_or_default();
    let port = if authority.starts_with('[') {
        authority.split_once(']')?.1.strip_prefix(':')?
    } else {
        authority.rsplit_once(':')?.1
    };
    port.parse().ok()
}

/// The gateway that goes with an entry URL: same scheme and host, gateway port.
pub fn derive_gateway(entry: &Url) -> Url {
    let mut gateway = entry.clone();
    gateway.set_path("");
    gateway.set_query(None);
    gateway.set_fragment(None);
    // set_port only fails for cannot-be-a-base URLs, which parse_entry rejects.
    let _ = gateway.set_port(Some(DEFAULT_GATEWAY_PORT));
    gateway
}

/// Resolve the configured settings into a usable endpoint pair, or `None` when
/// no server address has been configured yet (Android before onboarding).
pub fn resolve(settings: &Settings) -> Option<Endpoints> {
    let entry = parse_entry(settings.entry_url.as_deref()?)?;
    let gateway = settings
        .gateway_url
        .as_deref()
        .and_then(parse_entry)
        .unwrap_or_else(|| derive_gateway(&entry));
    Some(Endpoints { entry, gateway })
}

/// Resolve endpoints and, when requested by the platform, reject persisted
/// public cleartext targets too. Validation at save time protects the normal UI
/// path; this second boundary covers hand-edited or legacy `settings.json`.
pub fn resolve_checked(settings: &Settings, private_cleartext_only: bool) -> Option<Endpoints> {
    let endpoints = resolve(settings)?;
    if private_cleartext_only
        && (!is_private_host(&endpoints.entry) || !is_private_host(&endpoints.gateway))
    {
        return None;
    }
    Some(endpoints)
}

/// Whether a cleartext URL points somewhere it is safe to send a session
/// cookie: RFC1918, CGNAT/VPN-overlay (100.64.0.0/10), link-local, or loopback.
///
/// This is the policy Android's network security config is meant to carry but
/// cannot — `<domain>` takes a hostname or an IP literal, never a prefix — so
/// the app enforces it on the one cleartext origin it ever loads. `https`
/// URLs are exempt: TLS is the protection this stands in for.
///
/// A host that does not resolve is refused rather than assumed private: the
/// safe default for "cannot tell" is no.
// Called only from the Android build, but compiled and unit-tested on every
// host — the range arithmetic is exactly the part worth testing where the
// tests actually run.
#[cfg_attr(not(target_os = "android"), allow(dead_code))]
pub fn is_private_host(url: &Url) -> bool {
    use std::net::IpAddr;

    if url.scheme() == "https" {
        return true;
    }
    let Some(host) = url.host_str() else {
        return false;
    };
    let port = url.port_or_known_default().unwrap_or(80);
    let Ok(addresses) = (host, port).to_socket_addrs() else {
        return false;
    };
    let mut any = false;
    for address in addresses {
        any = true;
        let private = match address.ip() {
            IpAddr::V4(v4) => {
                v4.is_loopback()
                    || v4.is_private()
                    || v4.is_link_local()
                    // 100.64.0.0/10 — carrier-grade NAT, which is also the
                    // range some VPN overlays hand out to their nodes.
                    || (v4.octets()[0] == 100 && (64..128).contains(&v4.octets()[1]))
            }
            // Loopback, link-local (fe80::/10) and unique-local (fc00::/7).
            IpAddr::V6(v6) => {
                v6.is_loopback()
                    || (v6.segments()[0] & 0xffc0) == 0xfe80
                    || (v6.segments()[0] & 0xfe00) == 0xfc00
            }
        };
        // Every resolved address must be private: a name that also resolves to
        // a public address is a name that can send the cookie into the open.
        if !private {
            return false;
        }
    }
    any
}

/// Whether a same-window navigation is allowed to proceed.
///
/// HTTP(S) only, and the origin must be one of the configured endpoints.
/// Everything else is
/// cancelled and handed to the external opener. The app's own bundled pages
/// (onboarding, the unreachable screen) are served over the Tauri custom
/// protocol and are always allowed — they are the binary's own assets.
pub fn is_allowed_nav(url: &Url, endpoints: Option<&Endpoints>) -> bool {
    if is_app_asset(url) {
        return true;
    }
    if !matches!(url.scheme(), "http" | "https") {
        return false;
    }
    let Some(endpoints) = endpoints else {
        return false;
    };
    let origin = url.origin();
    origin == endpoints.entry.origin() || origin == endpoints.gateway.origin()
}

/// The app's own bundled assets, whose origin differs per platform
/// (`tauri://localhost` on macOS/Linux, `http://tauri.localhost` on Windows,
/// `http://tauri.localhost` on Android).
fn is_app_asset(url: &Url) -> bool {
    match url.scheme() {
        "tauri" | "asset" | "ipc" => true,
        "http" | "https" => matches!(url.host_str(), Some("tauri.localhost")),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(raw: &str) -> Url {
        Url::parse(raw).expect("test url")
    }

    #[test]
    fn bare_host_becomes_the_gate_url() {
        assert_eq!(
            parse_entry("box.local").map(|u| u.to_string()),
            Some("http://box.local:3000/".to_string())
        );
    }

    #[test]
    fn host_with_port_is_respected() {
        assert_eq!(
            parse_entry("100.64.3.2:3100").map(|u| u.to_string()),
            Some("http://100.64.3.2:3100/".to_string())
        );
    }

    #[test]
    fn full_url_passes_through() {
        assert_eq!(
            parse_entry("https://ava.example.com/").map(|u| u.to_string()),
            Some("https://ava.example.com/".to_string())
        );
    }

    #[test]
    fn server_address_normalization_uses_the_console_port() {
        assert_eq!(
            normalize_entry_address("100.103.96.72"),
            Ok("http://100.103.96.72:3000/".to_string())
        );
        assert_eq!(
            normalize_entry_address("http://box.local:3000"),
            Ok("http://box.local:3000/".to_string())
        );
        assert_eq!(
            normalize_entry_address("http://100.103.96.72:8000"),
            Ok("http://100.103.96.72:3000/".to_string())
        );
        assert_eq!(
            normalize_entry_address("https://box.local/login"),
            Ok("https://box.local:3000/".to_string())
        );
    }

    #[test]
    fn server_address_normalization_rejects_custom_ports_and_strips_routes() {
        assert_eq!(
            normalize_entry_address("http://box.local:3100"),
            Err(CUSTOM_ENTRY_PORT_ERROR.to_string())
        );
        assert_eq!(
            normalize_entry_address("http://box.local:8000/login?next=%2F#top"),
            Ok("http://box.local:3000/".to_string())
        );
    }

    #[test]
    fn gateway_override_normalization_mirrors_the_console_rules() {
        assert_eq!(
            normalize_gateway_address("box.local"),
            Ok("http://box.local:8000/".to_string())
        );
        assert_eq!(
            normalize_gateway_address("http://box.local:3000"),
            Ok("http://box.local:8000/".to_string())
        );
        assert_eq!(
            normalize_gateway_address("https://box.local:3100/api"),
            Ok("https://box.local:3100/".to_string())
        );
        assert_eq!(
            normalize_gateway_address("https://box.local:8000/pages/home"),
            Ok("https://box.local:8000/".to_string())
        );
    }

    #[test]
    fn non_http_schemes_are_rejected() {
        assert!(parse_entry("file:///etc/passwd").is_none());
        assert!(parse_entry("javascript:alert(1)").is_none());
        assert!(parse_entry("   ").is_none());
    }

    #[test]
    fn gateway_is_the_entry_host_on_the_gateway_port() {
        let entry = url("http://100.64.3.2:3000/some/path");
        assert_eq!(
            derive_gateway(&entry).to_string(),
            "http://100.64.3.2:8000/"
        );
    }

    #[test]
    fn pinned_gateway_wins_over_derivation() {
        let settings = Settings {
            entry_url: Some("http://box:3000".into()),
            gateway_url: Some("http://other:9000".into()),
            ..Settings::default()
        };
        let resolved = resolve(&settings).expect("resolved");
        assert_eq!(resolved.gateway.to_string(), "http://other:9000/");
    }

    #[test]
    fn unconfigured_entry_resolves_to_nothing() {
        let settings = Settings {
            entry_url: None,
            ..Settings::default()
        };
        assert!(resolve(&settings).is_none());
    }

    #[test]
    fn checked_resolution_refuses_a_persisted_public_cleartext_target() {
        let settings = Settings {
            entry_url: Some("http://1.1.1.1:3000".into()),
            gateway_url: Some("http://1.1.1.1:8000".into()),
            ..Settings::default()
        };
        assert!(resolve_checked(&settings, true).is_none());
        assert!(resolve_checked(&settings, false).is_some());
    }

    #[test]
    fn navigation_allows_only_the_configured_origins() {
        let endpoints = resolve(&Settings {
            entry_url: Some("http://box:3000".into()),
            ..Settings::default()
        })
        .expect("resolved");
        assert!(is_allowed_nav(
            &url("http://box:3000/agents"),
            Some(&endpoints)
        ));
        assert!(is_allowed_nav(
            &url("http://box:8000/api/system"),
            Some(&endpoints)
        ));
    }

    #[test]
    fn navigation_rejects_everything_else() {
        let endpoints = resolve(&Settings {
            entry_url: Some("http://box:3000".into()),
            ..Settings::default()
        })
        .expect("resolved");
        assert!(!is_allowed_nav(
            &url("https://github.com/anthropics"),
            Some(&endpoints)
        ));
        assert!(!is_allowed_nav(
            &url("http://evil.example/"),
            Some(&endpoints)
        ));
        assert!(!is_allowed_nav(
            &url("http://box:3001/not-the-gate"),
            Some(&endpoints)
        ));
        assert!(!is_allowed_nav(
            &url("https://box:3000/scheme-changed"),
            Some(&endpoints)
        ));
        assert!(!is_allowed_nav(
            &url("http://localhost:9999/unconfigured"),
            Some(&endpoints)
        ));
        assert!(!is_allowed_nav(
            &url("file:///etc/passwd"),
            Some(&endpoints)
        ));
    }

    #[test]
    fn app_assets_are_allowed_even_before_onboarding() {
        assert!(is_allowed_nav(&url("tauri://localhost/index.html"), None));
        assert!(is_allowed_nav(
            &url("http://tauri.localhost/index.html"),
            None
        ));
        assert!(!is_allowed_nav(&url("https://github.com/"), None));
    }

    #[test]
    fn cleartext_is_confined_to_private_space() {
        // Loopback and RFC1918 literals are what a cluster actually lives on.
        assert!(is_private_host(&url("http://127.0.0.1:3000/")));
        assert!(is_private_host(&url("http://192.168.1.10:3000/")));
        assert!(is_private_host(&url("http://10.1.2.3:3000/")));
        assert!(is_private_host(&url("http://172.16.0.9:3000/")));
        // 100.64.0.0/10 — the CGNAT / VPN-overlay range.
        assert!(is_private_host(&url("http://100.101.102.103:3000/")));
    }

    #[test]
    fn cleartext_to_a_public_address_is_refused() {
        assert!(!is_private_host(&url("http://1.1.1.1:3000/")));
        // 100.128.x is just outside 100.64.0.0/10 and must not be mistaken
        // for it — the boundary is the whole point of the mask.
        assert!(!is_private_host(&url("http://100.128.0.1:3000/")));
        assert!(!is_private_host(&url("http://172.32.0.1:3000/")));
    }

    #[test]
    fn tls_is_exempt_from_the_range_policy() {
        assert!(is_private_host(&url("https://ava.example.com/")));
    }

    #[test]
    fn an_unresolvable_host_is_refused_rather_than_assumed_private() {
        assert!(!is_private_host(&url("http://no-such-host.invalid:3000/")));
    }
}
