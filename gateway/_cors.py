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


def cors_allowed_origins() -> list[str]:
    """Return the exact browser origins allowed to call the gateway.

    An explicit allowlist is authoritative. Otherwise derive the local frontend
    origins and, when configured, the frontend port on the gateway URL's host.
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
    if ":" in hostname:
        hostname = f"[{hostname}]"
    gateway_origin = f"{parsed_gateway.scheme}://{hostname}:{frontend_port}"
    if gateway_origin not in origins:
        origins.append(gateway_origin)
    return origins


def session_cookie_secure() -> bool:
    """Return the effective Secure flag for gateway session cookies."""
    explicit = settings.gateway.session_cookie_secure
    if explicit is not None:
        return explicit
    return urlsplit(settings.gateway.gateway_url).scheme == "https"
