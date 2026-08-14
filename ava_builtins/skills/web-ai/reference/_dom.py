"""
DOM helpers for the web-ai driver: page-list id parsing, JS predicate
builders, and click-by-visible-text (accessibility-tree matching).

Split out of webchat.py (2026-08-07, Task #1011) so the driver entry stays
under the 800-line hard ceiling.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import ava

_SELECTED_RE = re.compile(r"^\s*(\d+):.*\[selected\]\s*$", re.MULTILINE)


def _selected_page_id(listing: str) -> int | None:
    """The numeric page id of the `[selected]` tab in a page-list dump, or None.

    new_page / list_pages render `  <id>: <url> [selected]` for the active tab;
    that id is what close_tab feeds back. None when no line is marked (format
    drift) -- callers treat that as "nothing safe to close" and leave tabs be.
    """
    m = _SELECTED_RE.search(listing)
    return int(m.group(1)) if m else None


def _any_present_js(selectors: list[str]) -> str:
    arr = json.dumps(selectors)
    return f"() => {{ for (const s of {arr}) {{ if (document.querySelector(s)) return true; }} return false; }}"


# --------------------------------------------------------------------------- #
# Generic page control matched by VISIBLE TEXT — for a button that has no stable
# selector (a "Deep Research" toggle, a "Start research" plan button). Text
# matching survives a redesign that only renames classes / data-attrs.
# --------------------------------------------------------------------------- #


# Roles a click target may carry in the rendered accessibility tree.
_CLICKABLE_ROLES = "button|menuitem|menuitemradio|menuitemcheckbox|link|radio|option|tab"
_SNAPSHOT_NODE_RE = re.compile(rf'uid=(\S+)\s+(?:{_CLICKABLE_ROLES})\b[^"\n]*"([^"]+)"')


def _resolve_clickable(texts: list[str]) -> tuple[str, str] | None:
    """(uid, label) of the first rendered control whose accessible name matches
    one of `texts`, case-insensitively. Exact pass over EVERY text before any
    substring pass: an early broad text must not substring-steal the match from
    a later text's exact hit ("Start" grabbing the "Start dictation" mic button
    while "Start research" sits right there). The substring pass requires a
    short label, guarding against a huge container that merely contains the
    word."""

    snapshot = ava.mcps.chrome.take_snapshot()
    nodes = [(uid, label.strip()) for uid, label in _SNAPSHOT_NODE_RE.findall(snapshot)]
    wants = [t.lower() for t in texts]
    hit = next((n for w in wants for n in nodes if n[1].lower() == w), None)
    if hit is None:
        hit = next(
            (n for w in wants for n in nodes if w in n[1].lower() and len(n[1]) < 60),
            None,
        )
    return hit


def download_by_click(
    texts: list[str], *, dest: Path, suffixes: tuple[str, ...], timeout: float = 60.0
) -> Path | None:
    """Click a control that triggers a browser download and collect the landed
    file from the system Downloads folder into `dest` (the full target path).
    The browser carries the session auth that a bare fetch of the asset URL
    does not. Returns None when the control wasn't found or no file landed in
    time (an in-flight download sits at .crdownload, so a suffix glob only
    sees completed files)."""
    import shutil

    downloads = Path.home() / "Downloads"
    before = {p.name for p in downloads.iterdir() if p.suffix in suffixes}
    if not click_by_text(texts):
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1.0)
        fresh = [p for p in downloads.iterdir() if p.suffix in suffixes and p.name not in before]
        if not fresh:
            continue
        newest = max(fresh, key=lambda p: p.stat().st_mtime)
        size = newest.stat().st_size
        time.sleep(1.0)
        if size > 0 and newest.exists() and newest.stat().st_size == size:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(newest), dest)
            return dest
    return None


def click_by_text(texts: list[str]) -> str | None:
    """Click the first rendered control whose accessible name matches one of
    `texts` (see `_resolve_clickable` for the matching rules). Returns the
    matched label, or None if nothing matched (caller decides whether that is
    fatal).

    Matching walks the rendered accessibility tree and clicking dispatches a
    real input event — NOT element.click(), which several of these apps'
    menus ignore (no synthetic-event handler on the trigger)."""

    hit = _resolve_clickable(texts)
    if hit is None:
        return None
    ava.mcps.chrome.click(uid=hit[0])
    return hit[1]
