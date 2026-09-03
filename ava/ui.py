"""Show the user a rich web page your HTTP server serves.

A page is declared with the platform, whose page-server daemon runs the
server inside a persistent shell session owned by this agent.
You can have at most one open page at a time — opening a new one
auto-closes the old one.

The page server binds the machine's own address (loopback on a single
machine) — it is never exposed on the network, and the page is reached
through the platform's authenticated link, not by dialing the server
directly.
"""

from __future__ import annotations

__all_for_ava__ = ["Page", "close", "serve", "show"]

import math as _math
import os as _os
import re as _re
import time as _time
import urllib.request as _urlopen
from dataclasses import dataclass
from pathlib import Path

import ava
import ava._boot
from ava import _gateway_client
from ava._sdk_validation import coerce_str, coerce_typed
from shared.machine import reachable_host

_NAME_RE = _re.compile(r"^[a-zA-Z0-9_-]+$")

# Default page port: derived from the agent id so each agent reuses one stable
# port across serves. Callers may override serve()/show() with an explicit port.
_PAGE_BASE_PORT = 10000

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


def _agent_page_port() -> int:
    """Return this agent's default page port."""
    return _PAGE_BASE_PORT + ava._boot.agent_id()


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


def _page_is_serving(host: str, port: int) -> bool:
    """Whether an HTTP server answers on (host, port) — the daemon's server
    (any token; identity is the daemon's concern, not the caller's)."""
    try:
        with _urlopen.urlopen(f"http://{host}:{port}/health", timeout=1.0) as resp:
            return resp.status == 200
    except OSError:
        return False


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
    port: int | None,
    title: str | None,
    serve_dir: str | None,
    *,
    ttl: float | None = None,
) -> Page:
    """Gateway registration shared by show() and serve().

    Closes any existing page first (one agent, one page), then writes the
    new row. `serve_dir` is the served directory the page_server daemon
    reads — only serve() sets it.
    """
    _validate_name(name)
    _close_existing()
    if port is None:
        port = _agent_page_port()
    if ttl is None:
        row = _gateway_client.register_page(
            ava._boot.agent_id(),
            name=name,
            port=port,
            host=reachable_host(),
            title=title,
            serve_dir=serve_dir,
        )
    else:
        row = _gateway_client.register_page(
            ava._boot.agent_id(),
            name=name,
            port=port,
            host=reachable_host(),
            title=title,
            serve_dir=serve_dir,
            ttl_seconds=int(ttl),
        )
    return _row_to_page(row)


def show(
    name: str,
    port: int | None = None,
    title: str | None = None,
    *,
    ttl: float | None = None,
) -> Page:
    """Show the user the page your HTTP server serves.

    Declares the page with the platform so the platform routes your
    server's URL — you started that server yourself; the platform's page
    supervisor does not manage it.

    Args:
        name: `^[a-zA-Z0-9_-]+$`, 1-64 chars.
        port: the port your server listens on; omit to use the port
            reserved for you.
        title: defaults to `name`.
        ttl: optional page lifetime in seconds; when omitted, the platform
            default applies. Expiry only unregisters the page (the link
            turns into the expired notice) — the server runs in your own
            process, so you must stop it yourself to release the port; the
            expiry notice reminds you.
    """
    name = coerce_str(name, "name")
    port = coerce_typed(port, "port", int, allow_none=True)
    title = coerce_str(title, "title", allow_none=True)
    ttl = coerce_typed(ttl, "ttl", (int, float), allow_none=True)
    return _register_page(name, port, title, serve_dir=None, ttl=_validate_ttl(ttl))


def serve(
    dir: str,
    name: str,
    port: int | None = None,
    title: str | None = None,
    *,
    ttl: float | None = None,
) -> Page:
    """Start an HTTP server for `dir` and show it to the user, in one call.

    Calling again auto-closes any existing page.
    A directory without `index.html` is not browsable: requests show a
    placeholder because directory listings are disabled. An `index.html` is
    required; for Markdown, render it to self-contained HTML first with
    the ava-ui markdown widget, then serve that directory.

    Args:
        dir: the directory to serve. A relative path is resolved against
            your working directory (`ava.cwd`), consistent with the
            `ava.files` API; `~` is expanded and an absolute path is used
            as-is.
        name: `^[a-zA-Z0-9_-]+$`, 1-64 chars.
        port: omit to use the port reserved for you.
        title: defaults to `name`.
        ttl: optional page lifetime in seconds; when omitted, the platform default applies.
    """
    dir = coerce_str(dir, "dir", allow_types=(_os.PathLike,))
    name = coerce_str(name, "name")
    port = coerce_typed(port, "port", int, allow_none=True)
    title = coerce_str(title, "title", allow_none=True)
    ttl = coerce_typed(ttl, "ttl", (int, float), allow_none=True)
    ttl = _validate_ttl(ttl)
    _validate_name(name)

    if port is None:
        port = _agent_page_port()

    _close_existing()

    page = _register_page(name, port, title, serve_dir=str(Path(dir).resolve()), ttl=ttl)

    # The daemon reconciles on a ~2s poll; wait for the server it spawns.
    if not _wait_until_serving(reachable_host(), port, timeout=_SERVE_READY_TIMEOUT_S):
        raise PageError(
            f"page server for {name!r} on port {port} did not come up within "
            f"{_SERVE_READY_TIMEOUT_S:.0f}s — is the page-server daemon running? "
            "(the page row is registered; the daemon will keep retrying)"
        )
    return page


def _close_existing() -> None:
    """Close the agent's currently active page, if any.

    Called before registering a new page so each agent has at most one
    open page at a time. Best-effort: if the existing page's server is
    already dead, still unregister it from the gateway. The DB row is the
    truth source — there is no in-process tracking anymore.
    """
    try:
        open_pages = _gateway_client.list_open_pages(ava._boot.agent_id())
    except Exception:
        return
    if not open_pages:
        return
    import contextlib as _cl

    name = open_pages[-1]["name"]  # most recent open page
    with _cl.suppress(Exception):
        close(name)  # fail-fast-ok: best-effort close; new page replaces it


def close(name: str) -> None:
    """Unregister the page (the platform stops its server)."""
    name = coerce_str(name, "name")
    _validate_name(name)

    try:
        _gateway_client.close_page(ava._boot.agent_id(), name)
    except Exception as e:
        # Gateway returns 404 -> httpx.HTTPStatusError. Translate to PageClosed
        # so callers can distinguish "already gone" from real errors.
        msg = str(e)
        if "404" in msg:
            raise PageClosed(f"no open page {name!r} for agent {ava._boot.agent_id()}") from e
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
