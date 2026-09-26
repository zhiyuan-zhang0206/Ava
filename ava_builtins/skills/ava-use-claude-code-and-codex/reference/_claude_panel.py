"""Claude Code panel predicates — what a PTY capture proves about the TUI.

Split out of ``spawn_claude.py`` (2026-09-26) so the launcher stays under the
800-line hard ceiling; it imports both predicates for its readiness wait.
"""

from __future__ import annotations

import re


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


_NOT_LOGGED_IN = re.compile(r"Not logged in\b[^\n]*/login")


def _check_logged_in(output: str) -> None:
    """A rendered but signed-out Claude cannot act; refuse before delivering text."""
    if _NOT_LOGGED_IN.search(output):
        raise RuntimeError(
            "Claude Code started but is not logged in on this host; run `claude auth login` "
            "as the host user, then launch again. No text was delivered"
        )
