"""Show the user a rich web page your HTTP server serves — one page at a time;
ports are explicit, and the page is reached only through the platform's authenticated link.
"""

from __future__ import annotations

__all_for_ava__ = ["Page", "close", "serve", "show"]

import math as _math
import os as _os
import re as _re
import socket as _socket
import time as _time
import urllib.request as _urlopen
from dataclasses import dataclass
from pathlib import Path

import ava
import ava.agent_identity
from ava import _gateway_client
from ava._sdk_validation import coerce_str, coerce_typed
from shared.machine import reachable_host

_NAME_RE = _re.compile(r"^[a-zA-Z0-9_-]+$")

# Page servers bind unprivileged ports (mirrors the gateway's dial-target
# guard — a page server is a user-space server, never a system service); the
# upper bound is the TCP port ceiling. Out-of-range ports fail at the call
# boundary so the gateway's 400 is never the first signal.
_PAGE_PORT_MIN = 1024
_PAGE_PORT_MAX = 65535

# How long serve() waits for the page_server daemon to bring the server up
# before failing. The daemon's fast path adopts a new row within one poll
# (~2s); this window is the fallback for a cold daemon restart or a slow pass
# under load — long enough to cover the slowest observed pass (~30s,
# 2026-08-28) while still failing loudly when the daemon is genuinely down.
_SERVE_READY_TIMEOUT_S = 60.0


class PageError(Exception):
    """Base for `ava.ui` failures — catch this for broad handling."""


class InvalidPageName(PageError):  # noqa: N818 — same style as AgentNotFound etc., no Error suffix
    """`name` failed the `^[a-zA-Z0-9_-]+$` check or length bound (1-64)."""


class PageClosed(PageError):  # noqa: N818
    """`close(name)` called but no open page with that name exists for this agent."""


@dataclass(frozen=True)
class Page:
    id: int
    name: str
    port: int
    title: str | None
    url: str


def _row_to_page(row: dict) -> Page:
    return Page(
        id=int(row["id"]),
        name=row["name"],
        port=int(row["port"]),
        title=row.get("title"),
        url=row["url"],
    )


def _validate_name(name: str) -> None:
    if not name or len(name) > 64 or not _NAME_RE.match(name):
        raise InvalidPageName(
            f"page name {name!r} invalid — must match ^[a-zA-Z0-9_-]+$ (1-64 chars); "
            "no slashes/dots/whitespace (URL path safety + stable identifier)"
        )


def _validate_ttl(ttl: float | None) -> float | None:
    if ttl is not None and (not _math.isfinite(ttl) or ttl <= 0):
        raise ValueError("ttl must be finite and greater than zero")
    return ttl


def _coerce_page_port(port: object) -> int:
    """Validate the required `port` argument (1024-65535).

    An explicit port is the only allocation mechanism — there is no per-agent
    reserved port — so a missing port is rejected with the rule spelled out
    instead of falling back to a computed value.
    """
    if port is None:
        raise TypeError(
            "port is required — pass the port the page server should listen on "
            f"({_PAGE_PORT_MIN}-{_PAGE_PORT_MAX}); ava.ui never allocates or reserves a port"
        )
    checked = coerce_typed(port, "port", int)
    if not _PAGE_PORT_MIN <= checked <= _PAGE_PORT_MAX:
        raise ValueError(
            f"port {checked} out of range — page servers bind unprivileged ports "
            f"({_PAGE_PORT_MIN}-{_PAGE_PORT_MAX})"
        )
    return checked


def _probe_page_health(host: str, port: int) -> str | None:
    """The body of (host, port)'s /health, or None when nothing answers 200."""
    try:
        with _urlopen.urlopen(f"http://{host}:{port}/health", timeout=1.0) as resp:
            if resp.status != 200:
                return None
            return resp.read().decode(errors="replace")
    except OSError:
        return None


def _page_is_serving(host: str, port: int) -> bool:
    """Whether an HTTP server answers on (host, port) — the daemon's server
    (any token; identity is the daemon's concern, not the caller's)."""
    return _probe_page_health(host, port) is not None


def _port_is_bindable(host: str, port: int) -> bool:
    """Whether a page server could bind (host, port) right now.

    A throwaway bind probe with SO_REUSEADDR set, mirroring the page server
    itself (services/page_server/server.py) — a TIME_WAIT remnant of an
    exited server must not read as occupied.
    """
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as probe:
        probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _answers_as_page_server(host: str, port: int) -> bool:
    """Whether (host, port) answers /health like this platform's page servers.

    A page server answers `ok:<token>` (services/page_server/server.py); any
    other occupant — a dev server, a database, a foreign HTTP service —
    answers differently or not at all.
    """
    body = _probe_page_health(host, port)
    return body is not None and body.startswith("ok:")


def _own_open_page_on_port(port: int) -> bool:
    """Whether this agent's current page row claims `port`.

    Best-effort read used only by the occupied-port guard: the agent's own
    page (whose server may still be exiting, or slow to answer) is replaced
    by registration, so it must not read as a foreign occupant. A gateway
    failure degrades to False — the registration call right after is the
    authority on conflicts.
    """
    try:
        pages = _gateway_client.list_open_pages(ava.agent_identity.agent_id())
    except Exception:
        return False
    return any(int(page["port"]) == port for page in pages)


def _reject_foreign_port_occupant(port: int) -> None:
    """Fail fast when the port is held by a process that is not a page server.

    serve() is about to have the page-server daemon bind this port; a foreign
    process can never be displaced (the daemon backs off and retries
    forever), so letting it through means a silent `_SERVE_READY_TIMEOUT_S`
    wait followed by a misleading "daemon down" error. Page-server occupants
    — this agent's own page being replaced, or another agent's page — pass
    through to the gateway's live-port conflict check, the only party that
    knows which page owns the port.

    Raises:
        PageError: the port is occupied by a non-page-server process.
    """
    host = reachable_host()
    if _port_is_bindable(host, port):
        return
    if _answers_as_page_server(host, port):
        return
    if _own_open_page_on_port(port):
        return
    raise PageError(
        f"port {port} is already in use on {host} by a process that is not a page "
        "server — choose a different free port (ava.ui.serve never allocates one)"
    )


def _wait_until_serving(host: str, port: int, *, timeout: float) -> bool:
    """Poll until the page server answers on (host, port) or timeout passes."""
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if _page_is_serving(host, port):
            return True
        _time.sleep(0.2)
    return False


def _register_page(
    name: str,
    port: int,
    title: str | None,
    serve_dir: str | None,
    *,
    ttl: float | None = None,
) -> Page:
    """Gateway registration shared by show() and serve().

    The gateway owns replacement (one page per agent — any existing page is
    closed as part of registering the new one) and the port rules: a port
    another live page holds is refused with 409, and the refusal leaves this
    agent's current page untouched. `serve_dir` is the served directory the
    page_server daemon reads — only serve() sets it.
    """
    _validate_name(name)
    try:
        if ttl is None:
            row = _gateway_client.register_page(
                ava.agent_identity.agent_id(),
                name=name,
                port=port,
                host=reachable_host(),
                title=title,
                serve_dir=serve_dir,
            )
        else:
            row = _gateway_client.register_page(
                ava.agent_identity.agent_id(),
                name=name,
                port=port,
                host=reachable_host(),
                title=title,
                serve_dir=serve_dir,
                ttl_seconds=int(ttl),
            )
    except Exception as exc:
        # 409 is the gateway's refusal: the agent is terminated, or another
        # live page holds (host, port). The wire body's `detail` names the
        # reason — raise it as the SDK's own error instead of a raw HTTP error.
        response = getattr(exc, "response", None)
        detail = _error_detail(exc) if getattr(response, "status_code", None) == 409 else None
        if detail is not None:
            raise PageError(detail) from exc
        raise
    return _row_to_page(row)


def _error_detail(exc: Exception) -> str | None:
    """The gateway error body's `detail` string, or None when there is none.

    Shape-based (a `response` attribute carrying the JSON body), not
    class-based: prod HTTP raises httpx's HTTPStatusError while the in-process
    TestClient raises httpx2's — the JSON wire body is the stable contract.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        parsed = response.json()
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    detail = parsed.get("detail")
    return detail if isinstance(detail, str) else None


def show(
    name: str,
    port: int,
    title: str | None = None,
    *,
    ttl: float | None = None,
) -> Page:
    """Show the user the page your HTTP server serves.

    Declares the page with the platform, which routes it to the user; the server
    stays yours — show() creates no session and does not probe whether the
    server answers, and expiry unregisters the page without stopping it.

    Args:
        name: `^[a-zA-Z0-9_-]+$`, 1-64 chars.
        port: the port your server listens on (1024-65535); required.
        title: defaults to `name`.
        ttl: page lifetime; omitted = the platform default; expiry does not stop the server.
    """
    name = coerce_str(name, "name")
    port = _coerce_page_port(port)
    title = coerce_str(title, "title", allow_none=True)
    ttl = coerce_typed(ttl, "ttl", (int, float), allow_none=True)
    return _register_page(name, port, title, serve_dir=None, ttl=_validate_ttl(ttl))


def serve(
    dir: str,
    name: str,
    port: int,
    title: str | None = None,
    *,
    ttl: float | None = None,
) -> Page:
    """Start an HTTP server for `dir` and show it to the user, in one call.

    The server runs inside a persistent shell session of this agent, listed as
    `page-<name>`; a new call auto-closes any existing page. End the page with
    `close()` or TTL expiry — killing that session only restarts the server, it
    does not close the page.

    `dir` must contain an `index.html` (render Markdown to self-contained HTML
    first); without it, requests show a placeholder — directory listings are
    disabled.

    Args:
        dir: relative paths resolve against your working directory (`ava.cwd`);
            `~` is expanded, an absolute path is used as-is.
        name: `^[a-zA-Z0-9_-]+$`, 1-64 chars.
        port: the page server's port (1024-65535); ava.ui never allocates or
            reserves one.
        title: defaults to `name`.
        ttl: page lifetime in seconds; omitted = the platform default.
    """
    dir = coerce_str(dir, "dir", allow_types=(_os.PathLike,))
    name = coerce_str(name, "name")
    port = _coerce_page_port(port)
    title = coerce_str(title, "title", allow_none=True)
    ttl = coerce_typed(ttl, "ttl", (int, float), allow_none=True)
    ttl = _validate_ttl(ttl)
    _validate_name(name)

    _reject_foreign_port_occupant(port)

    page = _register_page(name, port, title, serve_dir=str(Path(dir).resolve()), ttl=ttl)

    # The daemon reconciles on a ~2s poll; wait for the server it spawns.
    if not _wait_until_serving(reachable_host(), port, timeout=_SERVE_READY_TIMEOUT_S):
        raise PageError(
            f"page server for {name!r} on port {port} did not come up within "
            f"{_SERVE_READY_TIMEOUT_S:.0f}s — is the page-server daemon running? "
            "(the page row is registered; the daemon will keep retrying)"
        )
    return page


def close(name: str) -> None:
    """Unregister the page.

    For a `serve()` page, the platform also stops its server and ends its
    persistent shell session, removing the `page-<name>` session-list entry.
    For a `show()` page, only the registration ends; your server keeps running.
    """
    name = coerce_str(name, "name")
    _validate_name(name)

    try:
        _gateway_client.close_page(ava.agent_identity.agent_id(), name)
    except Exception as e:
        # Gateway returns 404 -> httpx.HTTPStatusError. Translate to PageClosed
        # so callers can distinguish "already gone" from real errors.
        msg = str(e)
        if "404" in msg:
            raise PageClosed(
                f"no open page {name!r} for agent {ava.agent_identity.agent_id()}"
            ) from e
        raise


def __getattr__(name: str) -> object:
    # Plugin members land on ava.ui via register_namespace_member (ava_fleet adds
    # notify / edit_notice / dismiss_notice). In an agent-launched
    # persistent-shell child they are absent until plugins load, and this module
    # already exists so ava.__getattr__ never fires — trigger the shared lazy
    # load here, then retry.
    import sys as _sys

    if ava._maybe_load_plugins_for_missing(name):
        return getattr(_sys.modules[__name__], name)
    raise AttributeError(f"module 'ava.ui' has no attribute {name!r}")
