"""Plugin-served pages — GET /api/plugin-ui/{plugin}/{path}.

The universal escape hatch of `future/frontend-plugin-contributions.md`: what a
plugin cannot express as a declaration, it ships as a page it serves itself,
and the console embeds that page in a sandboxed iframe. The declaration
vocabulary therefore never has to grow a widget type for every unforeseen need
(a task board, a balance panel, a pet).

This is the sibling mount of `gateway/routers/pages.py`, with the same path-
segment validation, the same auth (the cluster middleware, like every other
`/api` route), and the same 404-when-not-available semantics. What differs is
the backend: an `ava.ui` page is a live page server owned by an agent, while a
plugin page is **static files inside the plugin package** — `<plugin>/ui/`,
which converge already materializes along with the rest of the plugin image.
Static is the preferred backend precisely because it needs no process: nothing
to supervise, nothing to restart, nothing to leak.

Availability is the enable-state plus the directory: an ENABLED plugin that
ships a `ui/` directory has a mount, and nothing else does. There is no
manifest key to declare — a plugin that ships no pages ships no directory, and
the declarations under `contributions.ui` say where the console LINKS to a
page, not whether one is served.

The iframe the console wraps this in is breakage containment, not a security
boundary: plugin Python already runs in agent processes with shell and DB
access, so the trust decision was made at install time (the scan gate + trust
tier). What the boundary buys is that a broken or slow plugin page cannot take
down the console, and that no third-party code enters the console's bundle.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response

from shared import plugins_config, skill_names

router = APIRouter()

# The directory inside a plugin package whose contents are served.
PLUGIN_UI_DIR = "ui"

# Sent with every served file: a plugin page is third-party content, and
# content-type sniffing is how a file that claims to be one thing gets executed
# as another.
_STATIC_HEADERS = {"X-Content-Type-Options": "nosniff"}


def _enabled_plugin_dir(plugin: str) -> Path:
    """The plugin's package directory, once it is enabled and has a `ui/` dir.

    404 for every other case — unknown, disabled, or serving no pages — with
    the same detail, so the mount does not report the enable-state of a plugin
    to a caller that could not otherwise see it.
    """
    installed = plugins_config.installed_plugin_dirs()
    # Dash/underscore folding, exactly like `plugins_config.load`: the
    # directory is a Python package (`ava_code`) while the name a human writes
    # in a URL or a manifest is dash-spelled (`ava-code`).
    resolved = skill_names.find(plugin, set(installed)) or plugin
    directory = installed.get(resolved)
    if directory is None:
        raise HTTPException(status_code=404, detail=f"no plugin page mount for {plugin!r}")
    config = plugins_config.load_for_runtime(set(installed))
    entry = config.plugins.get(resolved)
    if entry is None or not entry.enabled:
        raise HTTPException(status_code=404, detail=f"no plugin page mount for {plugin!r}")
    ui_dir = directory / PLUGIN_UI_DIR
    if not ui_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"no plugin page mount for {plugin!r}")
    return ui_dir


def _validate_segments(rest: str) -> None:
    """Refuse a traversal spelled in the URL — `..`, backslashes, control
    characters, the same shapes `pages.py` refuses.

    Runs before anything touches the filesystem, so a crafted path cannot even
    probe whether a directory exists outside the mount.
    """
    for seg in rest.split("/"):
        if seg in (".", "..") or "\\" in seg or any(ord(c) < 0x20 for c in seg):
            raise HTTPException(status_code=400, detail=f"invalid page path segment {seg!r}")


def _resolve_file(ui_dir: Path, rest: str) -> Path:
    """The file `rest` names inside `ui_dir` — or 404.

    The companion guard to `_validate_segments`, and neither is redundant: the
    string check cannot see a symlink pointing out of the tree, and the
    containment check on the RESOLVED path is what refuses it.
    """
    candidate = ui_dir / rest if rest else ui_dir
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as e:
        raise HTTPException(status_code=404, detail=f"no such plugin page {rest!r}") from e
    root = ui_dir.resolve()
    if not resolved.is_relative_to(root):
        raise HTTPException(status_code=404, detail=f"no such plugin page {rest!r}")
    if resolved.is_dir():
        # A directory serves its index, the way a static host does — so a nav
        # entry can declare `board/` and the plugin can lay its page out as a
        # directory of assets. The caller redirects to the trailing-slash form
        # first; see `plugin_ui_get`.
        resolved = resolved / "index.html"
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail=f"no such plugin page {rest!r}")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"no such plugin page {rest!r}")
    return resolved


@router.get("/api/plugin-ui/{plugin}")
def plugin_ui_root(plugin: str) -> Response:
    """Trailing-slash canonicalization, so a page's relative asset links
    resolve against the mount rather than one level above it."""
    return RedirectResponse(f"/api/plugin-ui/{plugin}/", status_code=307)


@router.get("/api/plugin-ui/{plugin}/{rest:path}")
def plugin_ui_get(plugin: str, rest: str) -> Response:
    """Serve one file from an enabled plugin's `ui/` directory.

    A directory reached without a trailing slash redirects to the slash form
    before its index is served: the browser resolves a page's relative asset
    links against the URL it was loaded from, so `…/board` would send
    `app.js` to `…/app.js` — one level above where the plugin put it.
    """
    ui_dir = _enabled_plugin_dir(plugin)
    _validate_segments(rest)
    if rest and not rest.endswith("/") and (ui_dir / rest).is_dir():
        return RedirectResponse(f"/api/plugin-ui/{plugin}/{rest}/", status_code=307)
    path = _resolve_file(ui_dir, rest)
    media_type, _encoding = mimetypes.guess_type(path.name)
    return FileResponse(
        path,
        media_type=media_type or "application/octet-stream",
        headers=_STATIC_HEADERS,
    )
