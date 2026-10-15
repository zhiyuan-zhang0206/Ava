"""Gateway browser-origin and session-cookie transport policy."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit


def _frontend_port(frontend_healthcheck_url: str) -> int:
    parsed = urlsplit(frontend_healthcheck_url)
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme == "http":
        return 80
    if parsed.scheme == "https":
        return 443
    raise ValueError("AVA_FRONTEND_HEALTHCHECK_URL must include a port")


def _scheme_default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _origin_forms(scheme: str, hostname: str, port: int) -> list[str]:
    """All origin strings a browser may send for one (scheme, host, port).

    Browsers serialize the scheme's default port away, so a location whose
    port is the scheme default yields BOTH the bare form and the explicit
    default-port form — either can appear in an Origin header; any other
    port is always present, so only the explicit form is needed.
    """
    host = f"[{hostname}]" if ":" in hostname else hostname
    explicit = f"{scheme}://{host}:{port}"
    if port == _scheme_default_port(scheme):
        return [explicit, f"{scheme}://{host}"]
    return [explicit]


def _app_origins(port: int | None) -> list[str]:
    """Loopback origins of this cluster's own reserved Next.js app port.

    `AVA_APP_PORT` is the `app` slot start reserved in this home's registry
    record and wrote to its `.env` — the port the frontend service binds, so
    no caller predicts it. Next.js binds 127.0.0.1 only: a browser on this host
    reaches it as `127.0.0.1` or `localhost`; `[::1]` and every non-loopback
    host are never served and never allowed. Gate proxies this same listener
    under the loopback entry origins, so allowing the app's own origin trusts
    no content those do not; it serves a browser loading the app directly when
    Gate is not in the service selection. No reserved port derives nothing —
    never a guessed `entry + 1`.
    """
    if port is None:
        return []
    return [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]


def cors_allowed_origins(
    *,
    explicit: Sequence[str],
    frontend_healthcheck_url: str,
    app_port: int | None,
    browser_origin: str,
    gateway_url: str,
) -> list[str]:
    """Return the exact browser origins allowed to call the gateway.

    An explicit allowlist is authoritative. Otherwise derive the local
    frontend origins — the Gate entry and the reserved app port, on loopback
    only (`_app_origins`) — the same frontend entry on the gateway URL's host
    (the Origin the Gate UI sends when it lives on the gateway host but the
    frontend port), and the gateway URL's own origin — its scheme, host, and
    port — which is the Origin header a browser sends for same-origin
    requests to the gateway itself (e.g. the Grafana proxy under /grafana).
    """
    if explicit:
        return list(explicit)

    frontend_port = _frontend_port(frontend_healthcheck_url)
    origins = [
        f"http://localhost:{frontend_port}",
        f"http://127.0.0.1:{frontend_port}",
        *_app_origins(app_port),
    ]
    if browser_origin:
        origins.append(browser_origin)
    gateway_url = gateway_url.strip()
    if not gateway_url:
        return origins

    parsed_gateway = urlsplit(gateway_url)
    if not parsed_gateway.scheme or parsed_gateway.hostname is None:
        raise ValueError("AVA_GATEWAY_URL must be an absolute URL")
    hostname = parsed_gateway.hostname
    gateway_scheme = parsed_gateway.scheme
    gateway_port = (
        parsed_gateway.port
        if parsed_gateway.port is not None
        else _scheme_default_port(gateway_scheme)
    )

    for origin in (
        # The gateway URL's own origin, then the frontend entry on the same host.
        *_origin_forms(gateway_scheme, hostname, gateway_port),
        *_origin_forms(gateway_scheme, hostname, frontend_port),
    ):
        if origin not in origins:
            origins.append(origin)
    return origins


def session_cookie_secure(
    request_url: str = "", *, browser_origin: str, default_secure: bool
) -> bool:
    """Secure cookies on the configured HTTPS entry, preserving direct HTTP login.

    Uvicorn accepts forwarded scheme only from its trusted proxy peers. Merely
    sending an Origin header must not change a direct listener's cookie policy.
    """
    if browser_origin and request_url:
        request = urlsplit(request_url)
        browser = urlsplit(browser_origin)
        if (
            request.scheme == browser.scheme
            and request.hostname == browser.hostname
            and (request.port or 443) == (browser.port or 443)
        ):
            return True
    return default_secure
