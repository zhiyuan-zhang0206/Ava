"""Gateway browser-origin and session-cookie transport policy."""

from __future__ import annotations

from urllib.parse import urlsplit

from shared.config import settings


def _frontend_port() -> int:
    parsed = urlsplit(settings.services.frontend_healthcheck_url)
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


def cors_allowed_origins() -> list[str]:
    """Return the exact browser origins allowed to call the gateway.

    An explicit allowlist is authoritative. Otherwise derive the local
    frontend origins, the same frontend entry on the gateway URL's host (the
    Origin the Gate UI sends when it lives on the gateway host but the
    frontend port), and the gateway URL's own origin — its scheme, host, and
    port — which is the Origin header a browser sends for same-origin
    requests to the gateway itself (e.g. the Grafana proxy under /grafana).
    """
    explicit = settings.gateway.cors_allowed_origins
    if explicit:
        return explicit

    frontend_port = _frontend_port()
    origins = [
        f"http://localhost:{frontend_port}",
        f"http://127.0.0.1:{frontend_port}",
    ]
    gateway_url = settings.gateway.gateway_url.strip()
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


def session_cookie_secure() -> bool:
    """Return the effective Secure flag for gateway session cookies."""
    explicit = settings.gateway.session_cookie_secure
    if explicit is not None:
        return explicit
    return urlsplit(settings.gateway.gateway_url).scheme == "https"
