"""Show the user a rich web page your HTTP server serves.

A page is declared with the platform, and the platform supervises its
server process — the SDK never starts or tracks server processes itself.
You can have at most one open page at a time — opening a new one
auto-closes the old one.

The page server binds the machine's own address (loopback on a single
machine) — it is never exposed on the network, and the page is reached
through the platform's authenticated link, not by dialing the server
directly.
"""

from __future__ import annotations

__all_for_ava__ = ["Page", "close", "serve", "serve_markdown", "show"]

import functools as _functools
import re as _re
import shutil as _shutil
import time as _time
import urllib.request as _urlopen
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ava
import ava._boot
from ava import _gateway_client
from shared.machine import reachable_host

_NAME_RE = _re.compile(r"^[a-zA-Z0-9_-]+$")

# Default page port: derived from the agent id so each agent reuses one stable
# port across serves. Callers may override serve()/show() with an explicit port.
_PAGE_BASE_PORT = 10000

# How long serve() waits for the page_server daemon to bring the server up
# before failing (the daemon reconciles on a ~2s poll; the window covers a
# cold daemon restart too).
_SERVE_READY_TIMEOUT_S = 10.0

# Temp dirs created by serve_markdown(), keyed by page name so close() can
# clean them up. Lost on restart (in-process cache) — the daemon keeps serving
# the disk content; the leftover dir under /tmp is reclaimed by the OS.
_markdown_dirs: dict[str, str] = {}


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


def _markdown_widget_dir() -> Path:
    """Directory of the markdown widget shipped alongside the SDK.

    Lives under ``ava_builtins/skills/`` like every built-in skill asset, not
    repo-root ``skills/`` — the ava_builtins move (Ava-2200) is what broke the
    old relative path for serve_markdown.
    """
    return (
        Path(__file__).resolve().parent.parent
        / "ava_builtins"
        / "skills"
        / "ava-ui"
        / "widgets"
        / "markdown"
    )


@_functools.lru_cache(maxsize=1)
def _markdown_parser() -> Any:
    """The CommonMark + GFM parser shared by every serve_markdown page.

    Built once and reused; markdown-it-py is a CommonMark-compliant parser
    (unlike Python's `markdown`, whose fenced_code extension misses fenced
    blocks nested inside list items and whose default underscore rules
    treated intraword underscores as emphasis).
    """
    from markdown_it import MarkdownIt
    from mdit_py_plugins.gfm import gfm_plugin

    parser = MarkdownIt("commonmark", {"html": True, "linkify": True})
    parser.use(gfm_plugin)
    return parser


def _render_markdown(content: str) -> str:
    """Render CommonMark/GFM markdown to HTML for serve_markdown pages.

    LaTeX ``$...$``/``$$...$$`` is left untouched — KaTeX auto-render on
    the client does the math.
    """
    return _markdown_parser().render(content)


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


def _register_page(name: str, port: int | None, title: str | None, serve_dir: str | None) -> Page:
    """Gateway registration shared by show() and serve().

    Closes any existing page first (one agent, one page), then writes the
    new row. `serve_dir` is the served directory the page_server daemon
    reads — only serve()/serve_markdown() set it.
    """
    _validate_name(name)
    _close_existing()
    if port is None:
        port = _agent_page_port()
    row = _gateway_client.register_page(
        ava._boot.agent_id(),
        name=name,
        port=port,
        host=reachable_host(),
        title=title,
        serve_dir=serve_dir,
    )
    return _row_to_page(row)


def show(name: str, port: int | None = None, title: str | None = None) -> Page:
    """Show the user the page your HTTP server serves.

    Declares the page with the platform so the platform routes your
    server's URL — you started that server yourself; the platform's page
    supervisor does not manage it. Calling again auto-closes any existing
    page first — each agent has at most one open page.

    Args:
        name: `^[a-zA-Z0-9_-]+$`, 1-64 chars.
        port: the port your server listens on; omit to use the port
            reserved for you.
        title: defaults to `name`.
    """
    return _register_page(name, port, title, serve_dir=None)


def serve(dir: str, name: str, port: int | None = None, title: str | None = None) -> Page:
    """Start an HTTP server for `dir` and show it to the user, in one call.

    Declares the page (the content already lives in `dir`) and waits for
    the platform's page supervisor to spawn the server. The server binds
    the machine's own address (loopback on a single machine) — the page is
    only ever reached through the platform's authenticated link.

    Calling again auto-closes any existing page; `close(name)` closes the
    page and the platform stops the server.

    Args:
        name: `^[a-zA-Z0-9_-]+$`, 1-64 chars.
        port: omit to use the port reserved for you.
        title: defaults to `name`.
    """
    _validate_name(name)

    if port is None:
        port = _agent_page_port()

    _close_existing()

    page = _register_page(name, port, title, serve_dir=str(Path(dir).resolve()))

    # The daemon reconciles on a ~2s poll; wait for the server it spawns.
    if not _wait_until_serving(reachable_host(), port, timeout=_SERVE_READY_TIMEOUT_S):
        raise PageError(
            f"page server for {name!r} on port {port} did not come up within "
            f"{_SERVE_READY_TIMEOUT_S:.0f}s — is the page-server daemon running? "
            "(the page row is registered; the daemon will keep retrying)"
        )
    return page


def serve_markdown(
    content: str, name: str, port: int | None = None, title: str | None = None
) -> Page:
    """Serve Markdown as a rendered HTML page (LaTeX math, code highlighting,
    GFM tables) — same behavior as `serve`, including the port rules.

    Args:
        name: `^[a-zA-Z0-9_-]+$`, 1-64 chars.
        port: omit to use the port reserved for you.
        title: defaults to `name`.
    """
    _validate_name(name)

    # Locate the Markdown widget shipped alongside the SDK
    widget_dir = _markdown_widget_dir()
    template = (widget_dir / "md.html").read_text()

    # Build a temporary directory with the widget assets
    import tempfile as _tempfile

    tmpdir = Path(_tempfile.mkdtemp(prefix="ava_md_"))
    _shutil.copytree(widget_dir / "vendor", tmpdir / "vendor")

    # Server-side markdown -> HTML conversion (CommonMark-compliant)
    rendered = _render_markdown(content)
    # Also keep raw content (script-escaped) inside a hidden element for
    # KaTeX auto-render; the pre-rendered HTML already contains the math
    # delimiters, so the raw markdown is preserved for future use.
    safe = _re.sub(r"</(script)", r"<\/\1", content, flags=_re.I)
    html = template.replace("{{MARKDOWN_CONTENT}}", safe).replace("{{RENDERED_HTML}}", rendered)
    (tmpdir / "index.html").write_text(html)

    # Remove old dir for same name first (a re-serve replaces the old page)
    if name in _markdown_dirs:
        _shutil.rmtree(_markdown_dirs.pop(name), ignore_errors=True)

    # Defer tracking the new dir until after serve() succeeds, so that
    # _close_existing() -> close() does not delete the new tmpdir when
    # the page name is reused.
    result = serve(str(tmpdir), name, port, title)
    _markdown_dirs[name] = str(tmpdir)
    return result


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
    _validate_name(name)

    # Clean up any markdown temp dir created by serve_markdown()
    if name in _markdown_dirs:
        _shutil.rmtree(_markdown_dirs.pop(name), ignore_errors=True)

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
