"""Claude Code launch checks: panel readiness, the signed-out marker, and paste wrapping."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, cast

from meiva.shell import sessions

PASTE_WRAP_THRESHOLD_CHARS = 1022
"""One Claude composer chunk is ~1022 chars: a longer raw burst folds into collapsed paste fragments, and a submission made while such a fragment sits next to a raw tail drops the fragment's content (#4364)."""

PASTE_BEGIN = "\x1b[200~"
PASTE_END = "\x1b[201~"
_CLAUDE_NOT_FOUND = "claude executable not found in PATH or $HOME/.local/bin/claude"


def bracketed_paste(text: str) -> str:
    """Wrap a multi-chunk payload so the composer takes it as one atomic paste.

    The composer folds a raw burst longer than one chunk into collapsed paste
    fragments; when Enter lands with a collapsed fragment plus a raw tail in
    the composer, the fragment's content is dropped from the submission
    (#4364). Bracketed-paste markers make the composer take the whole payload
    as one paste whose content survives intact. Inner markers are stripped so
    the payload cannot close the wrapper early; a payload within one chunk is
    sent unchanged.
    """
    body = text.replace(PASTE_BEGIN, "").replace(PASTE_END, "")
    if len(body) <= PASTE_WRAP_THRESHOLD_CHARS:
        return body
    return f"{PASTE_BEGIN}{body}{PASTE_END}"


OWN_WORDS = " Follow the launch message pasted above: it is from the Meiva agent that launched you."
"""Typed after a pasted bootstrap. Claude Code marks a bracketed paste as pasted
content, and the model will not act on instructions that arrive only inside a
paste; it asks for confirmation instead, stalling an unattended session
(observed on Claude Code 2.1.283, 2026-09-27). One line of the operator's own
words, outside the paste, is the request to act on it."""


def send_bootstrap(sid: int, message: str) -> None:
    """Send a bootstrap message through the composer's paste path (#4364).

    A pasted bootstrap is followed by ``OWN_WORDS`` before the Enter; a
    bootstrap within one composer chunk is typed as-is and needs none.
    """
    payload = bracketed_paste(message)
    if payload.startswith(PASTE_BEGIN):
        payload += OWN_WORDS
    sessions.send(sid, payload)


def pretrust(workspace: Path) -> None:
    config_path = Path.home() / ".claude.json"
    ws_key = workspace.as_posix()
    data: dict[str, Any]
    try:
        data = cast(dict[str, Any], json.loads(config_path.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    projects = cast(dict[str, Any], data.setdefault("projects", {}))
    proj = cast(dict[str, Any], projects.setdefault(ws_key, {}))
    if proj.get("hasTrustDialogAccepted"):
        print(f"(already trusted: {ws_key})")
        return
    proj["hasTrustDialogAccepted"] = True
    config_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"+ trusted: {ws_key}")


def session_exists(name: str) -> bool:
    """Check if a persistent shell session with `name` already exists."""
    return any(existing_name == name for existing_name in sessions.list().values())


def claude_ui_ready(output: str, *, resumed: bool = False) -> bool:
    """Require the title and a composer cue, normalizing Unicode prompt spacing.

    An empty composer is a bare prompt glyph: captures strip trailing blanks,
    so the line may end right after it. A resumed session opens on its
    conversation history instead of the welcome banner, so the title is not
    required there; the composer cue (with its bypass-permissions footer) still
    is, which a shell prompt never shows.
    """
    normalized = re.sub(r"[^\S\r\n]", " ", output)
    composer = "? for shortcuts" in normalized or (
        bool(re.search(r"(?m)^ *\u276f(?: |\r?$)", normalized))
        and "bypass permissions on" in normalized
    )
    return composer and (resumed or bool(re.search(r"\bClaude\s+Code\b", normalized)))


def login_marker(failure_marker: Path) -> Path:
    """Written by the launch command when `claude auth status` reports signed out."""
    return failure_marker.with_name("not-logged-in")


def _check_login_marker(failure_marker: Path | None) -> None:
    if failure_marker is not None and login_marker(failure_marker).is_file():
        raise RuntimeError(
            "Claude Code is not logged in on this host; run `claude auth login` as the host "
            "user, then launch again. No text was delivered"
        )


def _check_missing_claude(failure_marker: Path | None, output: str = "") -> None:
    # A launched shell has a private marker. Its echoed command can wrap into
    # arbitrary screen lines, so its screen text is never failure evidence.
    missing = (
        failure_marker.is_file()
        if failure_marker is not None
        else bool(
            re.search(
                r"(?m)^error: claude executable not found in PATH or \$HOME/\.local/bin/claude\r?$",
                output,
            )
        )
    )
    if missing:
        print(f"error: {_CLAUDE_NOT_FOUND}")
        raise RuntimeError(f"Claude did not start: {_CLAUDE_NOT_FOUND}")


def wait_for_ready(
    sid: int,
    timeout: float = 30.0,
    *,
    failure_marker: Path | None = None,
    resumed: bool = False,
) -> None:
    """Require Claude's rendered UI before delivering any text to the session.

    Claude Code startup takes 5-10 seconds.  A fixed ``time.sleep(3)`` often
    sends the contract message before the TUI is ready, causing it to be lost.
    A long shell prompt is not a ready Claude panel.
    """
    print(f"waiting for session {sid} to be ready (polling capture)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        _check_missing_claude(failure_marker)
        try:
            output = sessions.capture(sid, scrollback=False)
        except ValueError as exc:
            _check_missing_claude(failure_marker)
            raise RuntimeError(
                f"Claude session {sid} exited before its UI appeared; check the executable "
                "in PATH or $HOME/.local/bin/claude"
            ) from exc
        _check_missing_claude(failure_marker, output)
        _check_login_marker(failure_marker)
        if claude_ui_ready(output, resumed=resumed):
            time.sleep(2)  # brief stability pause
            try:
                stable = sessions.capture(sid, scrollback=False)
            except ValueError as exc:
                _check_missing_claude(failure_marker)
                raise RuntimeError(f"Claude session {sid} exited before it became ready") from exc
            _check_missing_claude(failure_marker, stable)
            if claude_ui_ready(stable, resumed=resumed):
                print("  -> ready (Claude Code UI)")
                return
        time.sleep(1)
    raise RuntimeError(
        f"Claude Code UI did not appear in session {sid} within {timeout:.0f} s; "
        "no text was delivered"
    )
