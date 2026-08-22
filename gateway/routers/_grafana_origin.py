"""Canonical public-origin parsing for the Grafana Live boundary."""

from __future__ import annotations

from urllib.parse import urlsplit


def _origin_parts(value: str) -> tuple[str, str, int | None, str] | None:
    """Parse the origin fields shared by configured URLs and handshakes."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.hostname is None:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.query or parsed.fragment:
        return None
    return parsed.scheme, parsed.hostname.lower(), port, parsed.path


def normalized_origin(
    value: str,
    *,
    require_origin_form: bool,
) -> tuple[str, str, int] | None:
    parts = _origin_parts(value)
    if parts is None:
        return None
    scheme, hostname, port, path = parts
    if require_origin_form and path not in {"", "/"}:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname, port
