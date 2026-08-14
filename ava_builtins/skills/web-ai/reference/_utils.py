"""
Small leaf helpers for the web-ai driver: idle-detector state, chat-id
URL mapping, and the artifact landing dir.

Split out of webchat.py (2026-08-07, Task #1011) so the driver entry stays
under the 800-line hard ceiling.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path
from typing import Any

from _sites import site


def _new_idle_state() -> dict[str, Any]:
    """Fresh per-tab tracking for the idle detector (one streamed answer).

    `_idle_step` folds each poll into this; `done` flips on once the answer
    settles, carrying the final `answer` / `complete`. `last` is the newest text
    seen so far (the best partial to return on a timeout).
    """
    return {
        "last": "",  # newest answer text seen
        "stable_start": None,  # monotonic time the text went stable, or None
        "saw_stream": False,  # a stop/streaming control was seen at least once
        "growth_steps": 0,  # times the answer grew; >=2 means a real stream
        "answer": "",  # settled answer (set when done)
        "complete": False,  # whether it settled (vs still pending)
        "done": False,  # whether this tab is finished
    }


# --------------------------------------------------------------------------- #
# Chat ID: extract a stable conversation identifier so follow-up questions
# continue the SAME conversation instead of opening a new one.
# --------------------------------------------------------------------------- #


def chat_id_from_url(name: str, url: str) -> str | None:
    """Extract a stable chat identifier from the conversation ``url``.

    Returns the first capture group of the first matching
    ``chat_id_patterns`` regex, or None if no pattern matches.
    """
    prof = site(name)
    for pat in prof.get("chat_id_patterns", []):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def chat_url(name: str, chat_id: str) -> str:
    """Build the full conversation URL from a ``chat_id``.

    Uses the site's ``chat_url_template``; falls back to the raw ``chat_id``
    if no template is configured (it might already be a full URL).
    """
    prof = site(name)
    tmpl = prof.get("chat_url_template")
    if tmpl:
        return tmpl.format(chat_id=chat_id)
    return chat_id


# --------------------------------------------------------------------------- #
# Output landing: ~/Downloads/ava_<cluster>_web-ai/<capability>/<id>/ + stdout
# --------------------------------------------------------------------------- #

_GROUP = "web-ai"


def _cluster() -> str:
    """This unit's display label — its home's basename with leading dots
    stripped (path-only identity; `~/.ava` -> 'ava', `~/.ava-t1' -> 'ava-t1'),
    resolved without importing settings (no DB touch)."""
    home = Path(os.environ.get("AVA_HOME", "~/.ava")).expanduser()
    return home.name.lstrip(".") or "ava"


def downloads_root(capability: str) -> Path:
    """`~/Downloads/ava_<home-label>_web-ai/<capability>/` — the landing dir for a
    capability's artifacts, created on demand."""
    root = Path("~/Downloads").expanduser() / f"ava_{_cluster()}_{_GROUP}" / capability
    root.mkdir(parents=True, exist_ok=True)
    return root


def now_stamp() -> str:
    """Local-time `YYYYmmdd-HHMMSS` stamp for naming a run's output dir."""
    return _dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
