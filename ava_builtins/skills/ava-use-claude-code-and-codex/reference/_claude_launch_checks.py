"""Claude Code launch checks: panel readiness, the signed-out marker, and the
generation-scoped resident relay stub.

Split out of ``spawn_claude.py`` (2026-09-26) so the launcher stays under the
800-line hard ceiling; it imports these for its launch and readiness wait.
"""

from __future__ import annotations

import re
from pathlib import Path


def _claude_ui_ready(output: str) -> bool:
    """Require the title and a composer cue, normalizing Unicode prompt spacing.

    An empty composer is a bare prompt glyph: captures strip trailing blanks,
    so the line may end right after it.
    """
    normalized = re.sub(r"[^\S\r\n]", " ", output)
    composer = "? for shortcuts" in normalized or (
        bool(re.search(r"(?m)^ *\u276f(?: |\r?$)", normalized))
        and "bypass permissions on" in normalized
    )
    return bool(re.search(r"\bClaude\s+Code\b", normalized)) and composer


def _relay_stub_path(state_dir: Path) -> Path:
    """The resident relay credential stub of exactly one launch generation.

    `impersonate request` writes it 0600 and the plugin wrapper consumes it once.
    It lives in the generation's private state dir, never the shared workspace:
    a wrapper orphaned by an earlier session in the same workspace keeps
    polling its own (removed) generation dir and cannot steal this credential.
    """
    return state_dir / "relay.env"


def _login_marker(failure_marker: Path) -> Path:
    """Written by the launch command when `claude auth status` reports signed out."""
    return failure_marker.with_name("not-logged-in")


def _check_login_marker(failure_marker: Path | None) -> None:
    if failure_marker is not None and _login_marker(failure_marker).is_file():
        raise RuntimeError(
            "Claude Code is not logged in on this host; run `claude auth login` as the host "
            "user, then launch again. No text was delivered"
        )


def _generation_relay_stub(state_dir: Path | None) -> Path:
    """Create the new generation's private dir and name its stub inside it."""
    if state_dir is None:
        raise RuntimeError("new canonical owner is missing its generation state dir")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _relay_stub_path(state_dir)
