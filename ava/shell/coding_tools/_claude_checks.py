"""Claude Code launch checks: panel readiness, the signed-out marker, the
generation-scoped resident relay stub, and takeover start receipt."""

from __future__ import annotations

import contextlib
import json
import re
import time
from collections.abc import Callable
from pathlib import Path

import ava

_PASTE_WRAP_THRESHOLD_CHARS = 1022
"""One Claude composer chunk is ~1022 chars: a longer raw burst folds into collapsed paste fragments, and a submission made while such a fragment sits next to a raw tail drops the fragment's content (#4364)."""

_PASTE_BEGIN = "\x1b[200~"
_PASTE_END = "\x1b[201~"
_CLAUDE_NOT_FOUND = "claude executable not found in PATH or $HOME/.local/bin/claude"


def _bracketed_paste(text: str) -> str:
    """Wrap a multi-chunk payload so the composer takes it as one atomic paste.

    The composer folds a raw burst longer than one chunk into collapsed paste
    fragments; when Enter lands with a collapsed fragment plus a raw tail in
    the composer, the fragment's content is dropped from the submission
    (#4364). Bracketed-paste markers make the composer take the whole payload
    as one paste whose content survives intact. Inner markers are stripped so
    the payload cannot close the wrapper early; a payload within one chunk is
    sent unchanged.
    """
    body = text.replace(_PASTE_BEGIN, "").replace(_PASTE_END, "")
    if len(body) <= _PASTE_WRAP_THRESHOLD_CHARS:
        return body
    return f"{_PASTE_BEGIN}{body}{_PASTE_END}"


_OWN_WORDS = " Follow the launch message pasted above: it is from the Ava agent that launched you."
"""Typed after a pasted bootstrap. Claude Code marks a bracketed paste as pasted
content, and the model will not act on instructions that arrive only inside a
paste; it asks for confirmation instead, stalling an unattended takeover
(observed on Claude Code 2.1.283, 2026-09-27). One line of the operator's own
words, outside the paste, is the request to act on it."""


def _send_bootstrap(sid: int, message: str) -> None:
    """Send the takeover bootstrap through the composer's paste path (#4364).

    A pasted bootstrap is followed by ``_OWN_WORDS`` before the Enter; a
    bootstrap within one composer chunk is typed as-is and needs none.
    """
    payload = _bracketed_paste(message)
    if payload.startswith(_PASTE_BEGIN):
        payload += _OWN_WORDS
    ava.shell.sessions.send(sid, payload)


def _pretrust(workspace: Path) -> None:
    config_path = Path.home() / ".claude.json"
    ws_key = workspace.as_posix()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    projects = data.setdefault("projects", {})
    proj = projects.setdefault(ws_key, {})
    if proj.get("hasTrustDialogAccepted"):
        print(f"(already trusted: {ws_key})")
        return
    proj["hasTrustDialogAccepted"] = True
    config_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"+ trusted: {ws_key}")


def _session_exists(name: str) -> bool:
    """Check if a persistent shell session with `name` already exists."""
    return any(existing_name == name for existing_name in ava.shell.sessions.list().values())


def _claude_ui_ready(output: str, *, resumed: bool = False) -> bool:
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


def _wait_for_ready(
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
            output = ava.shell.sessions.capture(sid, scrollback=False)
        except ValueError as exc:
            _check_missing_claude(failure_marker)
            raise RuntimeError(
                f"Claude session {sid} exited before its UI appeared; check the executable "
                "in PATH or $HOME/.local/bin/claude"
            ) from exc
        _check_missing_claude(failure_marker, output)
        _check_login_marker(failure_marker)
        if _claude_ui_ready(output, resumed=resumed):
            time.sleep(2)  # brief stability pause
            try:
                stable = ava.shell.sessions.capture(sid, scrollback=False)
            except ValueError as exc:
                _check_missing_claude(failure_marker)
                raise RuntimeError(f"Claude session {sid} exited before it became ready") from exc
            _check_missing_claude(failure_marker, stable)
            if _claude_ui_ready(stable, resumed=resumed):
                print("  -> ready (Claude Code UI)")
                return
        time.sleep(1)
    raise RuntimeError(
        f"Claude Code UI did not appear in session {sid} within {timeout:.0f} s; "
        "no text was delivered"
    )


_BOOTSTRAP_MARKER = "take over Ava agent"


def _bootstrap_count(claude_session: str) -> int:
    """How often this session's own transcript records the takeover bootstrap.

    Claude Code appends ``~/.claude/projects/<slug>/<session id>.jsonl`` as the
    executor works, so a count above the one taken before the send is
    submission evidence. Visibility alone is not: a message parked in the
    composer shows the same text (F1 class). A resumed session's transcript
    already holds its earlier bootstrap, which is why this counts, not finds.
    """
    count = 0
    for path in (Path.home() / ".claude" / "projects").glob(f"*/{claude_session}.jsonl"):
        with contextlib.suppress(OSError):
            count += path.read_text(encoding="utf-8", errors="ignore").count(_BOOTSTRAP_MARKER)
    return count


def _verify_start_receipt(
    sid: int,
    rebuild_bootstrap: Callable[[], str],
    submitted: Callable[[], bool],
    timeout: float = 45.0,
) -> None:
    """The takeover bootstrap must actually submit, not vanish into the composer.

    A live session is not receipt: a message parked in the composer leaves an
    executor that never learned it replaced the agent (F1 class — do not infer
    receipt from a zero exit code). Submission evidence is ``submitted()``:
    the session's own transcript records one more bootstrap than it did before
    the send (``_bootstrap_count``). When it stays absent,
    press Enter once (a stale composer entry submits there) and re-check. If
    that still lacks evidence, rebuild and resend the formal bootstrap once.
    The final evidence re-check immediately before rebuilding avoids a duplicate
    during delayed transcript writes; the straight-line recovery branch invokes
    the factory at most once. Canonical session ownership already excludes a
    second launcher for the same workspace. Kept loud but not fatal: the
    session may still be rendering; the operator sees the warning. A dead
    session's send_keys()/send()/capture() refusal is reported the same way.
    """
    print("verifying the takeover bootstrap was submitted...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if submitted():
            print("  -> start-receipt=submitted (transcript)")
            return
        time.sleep(2)
    print("  -> no submission evidence yet; sending Enter once")
    try:
        ava.shell.sessions.send_keys(sid, "Enter")
    except ValueError as exc:
        print(
            "  -> WARNING: start-receipt=not-submitted "
            f"(Enter retry failed: {exc}); the session may have ended. "
            "Check the session before relying on it."
        )
        return
    time.sleep(5)
    if submitted():
        print("  -> start-receipt=submitted after Enter retry")
        return
    if submitted():
        print("  -> start-receipt=submitted before rebuild resend")
        return
    print("  -> no submission evidence after Enter; rebuilding and resending once")
    message = rebuild_bootstrap()
    try:
        _send_bootstrap(sid, message)
    except ValueError as exc:
        print(
            "  -> WARNING: start-receipt=not-submitted "
            f"(rebuild resend failed: {exc}); the session may have ended. "
            "Check the session before relying on it."
        )
        return
    time.sleep(5)
    if submitted():
        print("  -> start-receipt=submitted after rebuild resend")
        return
    try:
        visible = _BOOTSTRAP_MARKER in ava.shell.sessions.capture(sid)
    except ValueError as exc:
        # capture() refuses a dead session with ValueError; that refusal must
        # not bypass this checkpoint's loud-not-fatal contract (the caller kills
        # the session and rolls the launch back on exceptions).
        print(
            "  -> WARNING: start-receipt=not-submitted "
            f"(rebuild resend completed; capture failed: {exc}); the takeover bootstrap "
            "may be parked, or the session may have ended. Check the session before "
            "relying on it."
        )
        return
    print(
        "  -> WARNING: start-receipt=not-submitted "
        f"(after one rebuild resend; visible={visible}); the takeover bootstrap may be "
        "parked or missing. Check the session before relying on it."
    )
