#!/usr/bin/env python3
"""Create, adopt, inspect, or stop the canonical Claude Code workspace generation.

The active identity is ``(cluster, canonical workspace, claude)``. A supervised
worker (the default) keeps the two-file collaboration — a task file, a work
file, and a session the launcher's owner follows with ``watch_work.py``. A
takeover (``--impersonate-self``) runs file- and supervisor-less with its
briefing inlined in the launch message: its relay starts with the session
via the bundled ava-relay plugin (resident mode; ``--no-relay-resident``
restores the executor-armed Monitor flow). A concurrent or
cross-agent caller adopts the live record instead of stacking another Claude
process.

Usage::

    .venv/bin/python spawn_claude.py <workspace-dir>
    .venv/bin/python spawn_claude.py <workspace-dir> --work-file notes/progress.md
    .venv/bin/python spawn_claude.py <workspace-dir> --impersonate-self \
        --impersonation-name 'Fix login' --brief '<the full briefing text>'

Supervised mode pre-trusts the directory in ``~/.claude.json`` so no trust
prompt blocks the session, creates the task file / work file if absent (parent
dirs included), creates a persistent shell session named ``claude-<dirname>``
and launches ``claude --dangerously-skip-permissions`` (after ``unset
ANTHROPIC_API_KEY`` to avoid API-key billing trap), then polls
``ava.shell.sessions.capture`` until Claude Code has rendered its UI and sends
the collaboration-contract message — which names both file paths, so the
coding agent is told where they are rather than assuming a layout.

A takeover instead publishes a generation-owned record under the canonical key,
opens the session under the record's name, and sends the inline briefing; it
reads no task or work file, writes nothing in the workspace beyond the trust
flag, and starts no supervisor. ``--status`` / ``--cancel-generation`` inspect
or stop the record (supervised launches stay outside the record plane by
design, so those two report none for them).

The workspace is any directory: a git worktree, a scratch folder, a checkout of
an unrelated project. Supervised mode imposes no structure on it beyond the two
files, and both of those are relocatable via the flags below.

Output: owner-record fields (``adopted`` / ``session_id`` / ``generation`` …)
for a takeover; ``session_id``, ``tasks_file``, ``work_file`` for a supervised
launch (one ``key=value`` per line). Feed ``work_file`` straight into
``watch_work.py``'s ``WORK_FILE``; do not reconstruct the path from a
convention. Interact via ``ava.shell.sessions.send`` /
``ava.shell.sessions.send_keys`` / ``ava.shell.sessions.capture`` /
``ava.shell.sessions.kill``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shlex
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import ava
from shared import coding_session_owner
from shared.agents import AgentNotFound, AgentStatus

# The first-run presets live in a sibling module (moved out when this launcher
# crossed the 800-line budget). Script mode drops the script's own directory
# from sys.path under PYTHONSAFEPATH=1, and importlib path-loads never had it,
# so restore it before the sibling import.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _claude_first_run import _preset_claude_first_run  # noqa: E402

_DEFAULT_TTL_SECONDS = 24 * 3600

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


def _send_bootstrap(sid: int, message: str) -> None:
    """Send the takeover bootstrap through the composer's paste path (#4364)."""
    ava.shell.sessions.send(sid, _bracketed_paste(message))


def _resolve_dir(dir_path: str) -> Path:
    p = Path(dir_path).expanduser().resolve()
    if not p.is_dir():
        print(f"error: {p} is not a directory or does not exist", file=sys.stderr)
        sys.exit(1)
    return p


def _resolve_file(workspace: Path, raw: str) -> Path:
    """Absolute path stays as given; a relative one is taken against the workspace."""
    p = Path(raw).expanduser()
    return p.resolve() if p.is_absolute() else (workspace / p).resolve()


def _init_file(path: Path, initial: str) -> None:
    """Create the file with `initial` if it does not exist yet, parents included."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(initial, encoding="utf-8")


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


def _contract_path() -> Path:
    # This script lives in skills/ava-use-other-agents/reference/;
    # the collaboration protocol is in the same directory.
    return Path(__file__).resolve().parent / "collaboration_protocol.md"


def _claude_ui_ready(output: str) -> bool:
    """Require the title and a composer cue, normalizing Unicode prompt spacing."""
    normalized = re.sub(r"[^\S\r\n]", " ", output)
    composer = "? for shortcuts" in normalized or (
        bool(re.search(r"(?m)^ *\u276f ", normalized)) and "bypass permissions on" in normalized
    )
    return bool(re.search(r"\bClaude\s+Code\b", normalized)) and composer


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


def _wait_for_ready(sid: int, timeout: float = 30.0, *, failure_marker: Path | None = None) -> None:
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
        if _claude_ui_ready(output):
            time.sleep(2)  # brief stability pause
            try:
                stable = ava.shell.sessions.capture(sid, scrollback=False)
            except ValueError as exc:
                _check_missing_claude(failure_marker)
                raise RuntimeError(f"Claude session {sid} exited before it became ready") from exc
            _check_missing_claude(failure_marker, stable)
            if _claude_ui_ready(stable):
                print("  -> ready (Claude Code UI)")
                return
        time.sleep(1)
    raise RuntimeError(
        f"Claude Code UI did not appear in session {sid} within {timeout:.0f} s; "
        "no text was delivered"
    )


def _bootstrap_submitted(started: float) -> bool:
    """Did a Claude session transcript freshly record the takeover bootstrap?

    Claude Code appends session transcripts under ``~/.claude/projects/<slug>/``
    as the executor works; the bootstrap line appearing in a transcript written
    after the send is submission evidence. Visibility alone is not: a message
    parked in the composer shows the same text (F1 class).
    """
    projects = Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        return False
    cutoff = started - 10.0
    for path in projects.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            if "take over Ava agent" in path.read_text(encoding="utf-8", errors="ignore"):
                return True
        except OSError:
            continue
    return False


def _verify_start_receipt(
    sid: int, rebuild_bootstrap: Callable[[], str], timeout: float = 45.0
) -> None:
    """The takeover bootstrap must actually submit, not vanish into the composer.

    A live session is not receipt: a message parked in the composer leaves an
    executor that never learned it replaced the agent (F1 class — do not infer
    receipt from a zero exit code). Submission evidence = the workspace's
    Claude transcript freshly records the bootstrap. When it stays absent,
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
    started = time.time()
    deadline = started + timeout
    while time.time() < deadline:
        if _bootstrap_submitted(started):
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
    if _bootstrap_submitted(started):
        print("  -> start-receipt=submitted after Enter retry")
        return
    if _bootstrap_submitted(started):
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
    if _bootstrap_submitted(started):
        print("  -> start-receipt=submitted after rebuild resend")
        return
    try:
        visible = "take over Ava agent" in ava.shell.sessions.capture(sid)
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


def _owner_terminated(agent_id: int) -> bool:
    try:
        return ava.agents.get_status(agent_id) is AgentStatus.TERMINATED
    except AgentNotFound:
        return True
    except Exception:
        # An unavailable gateway cannot prove an owner dead. The supervisor and
        # expiry retain responsibility; guessing here could kill active work.
        return False


_RELAY_STUB_NAME = ".ava-relay.env"
"""Resident relay credential stub: `impersonate request` writes it 0600; the wrapper consumes it once."""


def _relay_stub_path(workspace: Path) -> Path:
    """The per-session relay credential stub; cleared before each takeover launch."""
    return workspace / _RELAY_STUB_NAME


def _relay_options(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> tuple[bool, Path | None]:
    """Resolve the resident-relay flags; takeover-only, and the group keeps the two exclusive."""
    if not args.impersonate_self and (
        args.no_relay_resident or args.relay_resident_dir is not None
    ):
        parser.error("--no-relay-resident/--relay-resident-dir require --impersonate-self")
    plugin_dir = Path(args.relay_resident_dir) if args.relay_resident_dir else None
    return not args.no_relay_resident, plugin_dir


def _claude_command(
    workspace: Path,
    caller_instance: str | None = None,
    *,
    failure_marker: Path | None = None,
    relay_plugin_dir: Path | None = None,
) -> str:
    from shared.external_caller import launch_caller_assignment

    resident = ""
    plugin_flag = ""
    if relay_plugin_dir is not None:
        stub = _relay_stub_path(workspace)
        resident = (
            f"export AVA_IMPERSONATION_RELAY_STUB={shlex.quote(stub.as_posix())} "
            f"AVA_IMPERSONATION_RELAY_PY={shlex.quote(sys.executable)} && "
        )
        plugin_flag = f" --plugin-dir {shlex.quote(relay_plugin_dir.as_posix())}"
    mark_failure = (
        f"printf '%s\\n' 'claude executable not found' > {shlex.quote(failure_marker.as_posix())}; "
        if failure_marker is not None
        else ""
    )
    return (
        f"cd {shlex.quote(workspace.as_posix())} && "
        "unset ANTHROPIC_API_KEY && "
        f"{resident}"
        "claude_bin=$(command -v claude); "
        'if [ -z "$claude_bin" ]; then claude_bin="$HOME/.local/bin/claude"; fi; '
        'if [ ! -x "$claude_bin" ]; then '
        f"{mark_failure}"
        "printf '%s\\n' 'error: claude executable not found in PATH or "
        "$HOME/.local/bin/claude' >&2; exit 127; fi; "
        f"{launch_caller_assignment('claude_code', caller_instance)}"
        f'exec "$claude_bin" --dangerously-skip-permissions{plugin_flag} || exit $?'
    )


def _takeover_bootstrap_message(
    agent_id: int, name: str, brief: str, *, relay_resident: bool
) -> str:
    """Inline the briefing; a takeover reads no task or work file."""
    from ava._impersonation_launch import bootstrap_message

    guide = (
        Path(__file__).resolve().parents[4]
        / ".agents"
        / "skills"
        / "impersonator-guide"
        / "SKILL.md"
    )
    return bootstrap_message(agent_id, name, "claude", brief, guide, relay_resident=relay_resident)


def _print_owner(owner: coding_session_owner.CodingSessionOwner, *, adopted: bool) -> None:
    print(f"adopted={'true' if adopted else 'false'}")
    print(f"status={owner.status}")
    if owner.generation is not None:
        print(f"generation={owner.generation}")
    if owner.owner_agent_id is not None:
        print(f"owner_agent_id={owner.owner_agent_id}")
    if owner.session_id is not None:
        print(f"session_id={owner.session_id}")
    if owner.session_name is not None:
        print(f"session_name={owner.session_name}")
    if owner.state_dir is not None:
        print(f"state_dir={owner.state_dir}")
    if owner.tasks_file is not None:
        print(f"tasks_file={owner.tasks_file}")
    if owner.work_file is not None:
        print(f"work_file={owner.work_file}")


def _status(key: coding_session_owner.CodingSessionKey) -> int:
    owner = coding_session_owner.read(key)
    _print_owner(owner, adopted=False)
    if owner.error:
        print(f"error={owner.error}", file=sys.stderr)
    return 1 if owner.status == "invalid" else 0


def _cancel(key: coding_session_owner.CodingSessionKey, generation: str) -> int:
    stopped = coding_session_owner.terminate_generation(
        key,
        generation,
        reason="explicit-cancel",
    )
    if not stopped:
        print("cancel refused: generation is not the current canonical owner", file=sys.stderr)
        return 1
    _print_owner(coding_session_owner.read(key), adopted=False)
    return 0


def _claim_canonical(
    key: coding_session_owner.CodingSessionKey,
    *,
    tasks_file: Path | None,
    work_file: Path | None,
    ttl_seconds: float,
) -> coding_session_owner.CodingSessionClaim:
    """Create or adopt, waiting through another claimant's bounded launch."""
    previous = coding_session_owner.read(key)
    terminated_generation = None
    if (
        previous.generation is not None
        and previous.owner_agent_id is not None
        and _owner_terminated(previous.owner_agent_id)
    ):
        terminated_generation = previous.generation
    while True:
        result = coding_session_owner.claim(
            key,
            owner_agent_id=ava.self.AGENT_ID,
            tasks_file=tasks_file,
            work_file=work_file,
            ttl_seconds=ttl_seconds,
            terminated_generation=terminated_generation,
        )
        if result.action != "busy":
            return result
        time.sleep(0.25)


def _run_supervised_launch(
    workspace: Path,
    tasks_file: Path,
    work_file: Path,
    ttl_seconds: float,
    caller_instance: str | None,
) -> int:
    _init_file(tasks_file, "")
    _init_file(work_file, "STATUS: WORKING\n\n## Log\n\n")
    _pretrust(workspace)

    session_name = f"claude-{workspace.name}"
    if _session_exists(session_name):
        print(
            f"session '{session_name}' already exists. session id: see `ava.shell.sessions.list()`"
        )
        return 1

    # Create a persistent shell session visible in the Inspect panel. TTL is
    # mandatory (2026-08-27 ruling); the default 24h is a generous cap for a
    # coding session — the supervisor re-spawns the script if a session is ever
    # reclaimed.
    sid = ava.shell.sessions.new(name=session_name, ttl=ttl_seconds)
    try:
        with tempfile.TemporaryDirectory(prefix="ava-claude-launch-") as marker_dir:
            marker = Path(marker_dir) / "missing-claude"
            ava.shell.sessions.send(
                sid, _claude_command(workspace, caller_instance, failure_marker=marker)
            )
            print(f"+ persistent shell session: {sid} ({session_name})")
            _wait_for_ready(sid, failure_marker=marker)

        contract = _contract_path()
        msg = (
            f"Read the collaboration contract at {contract} and follow it. "
            f"Your workspace is {workspace}. "
            f"Your task file (read-only for you) is {tasks_file}. "
            f"Your work file (yours to write, STATUS + log) is {work_file}. "
            "Now read the task file and start working."
        )
        ava.shell.sessions.send(sid, msg)
    except BaseException:
        with contextlib.suppress(Exception):
            ava.shell.sessions.kill(sid)
        raise

    print(f"ready. name={session_name}  workspace={workspace}")
    print(f"session_id={sid}")
    print(f"tasks_file={tasks_file}")
    print(f"work_file={work_file}")
    print(
        "NOTE: for xhigh effort on >10 files, consider STALL_SECONDS=1200+ "
        "in watch_work.py to avoid false stall alerts."
    )
    return 0


def _run_takeover_launch(
    workspace: Path,
    takeover_name: str,
    takeover_brief: str,
    ttl_seconds: float,
    caller_instance: str | None,
    *,
    relay_resident: bool = True,
    relay_plugin_dir: Path | None = None,
) -> int:
    plugin_dir: Path | None = None
    if relay_resident:
        candidate = relay_plugin_dir or _HERE / "ava-relay"
        if not candidate.is_dir():
            raise RuntimeError(
                f"the relay plugin directory is missing: {candidate}; "
                "launch with --no-relay-resident to use the executor-armed flow"
            )
        plugin_dir = candidate.resolve()
    key = coding_session_owner.canonical_key(workspace, tool="claude")
    claim = _claim_canonical(
        key,
        tasks_file=None,
        work_file=None,
        ttl_seconds=ttl_seconds,
    )
    if claim.action == "adopt":
        raise RuntimeError(
            "a takeover needs a fresh coding workspace; this workspace already has a live "
            "generation - cancel it with --cancel-generation first"
        )
    owner = claim.owner
    if owner.generation is None or owner.expected_suffix is None or owner.owner_agent_id is None:
        raise RuntimeError("new canonical owner is missing launch fields")
    generation = owner.generation
    expected_suffix = owner.expected_suffix
    owner_agent_id = owner.owner_agent_id
    sid: int | None = None
    try:
        if plugin_dir is not None:
            stub = _relay_stub_path(workspace)
            stub.unlink(missing_ok=True)  # never consume a stale credential
            Path(f"{str(stub).removesuffix('.env')}.pid").unlink(missing_ok=True)
        _pretrust(workspace)
        sid = ava.shell.sessions.new(name=expected_suffix, ttl=ttl_seconds)
        full_name = coding_session_owner.full_session_name(owner_agent_id, sid, expected_suffix)
        active = coding_session_owner.publish_active(
            key,
            generation,
            session_id=sid,
            session_name=full_name,
        )
        with tempfile.TemporaryDirectory(prefix="ava-claude-launch-") as marker_dir:
            marker = Path(marker_dir) / "missing-claude"
            ava.shell.sessions.send(
                sid,
                _claude_command(
                    workspace,
                    caller_instance,
                    failure_marker=marker,
                    relay_plugin_dir=plugin_dir,
                ),
            )
            _wait_for_ready(sid, failure_marker=marker)
        message = _takeover_bootstrap_message(
            owner_agent_id, takeover_name, takeover_brief, relay_resident=plugin_dir is not None
        )
        _send_bootstrap(sid, message)
        _verify_start_receipt(
            sid,
            lambda: _takeover_bootstrap_message(
                owner_agent_id, takeover_name, takeover_brief, relay_resident=plugin_dir is not None
            ),
        )
    except BaseException:
        # A replacement may own the canonical record by now, so its generation
        # CAS cannot reclaim this PTY. The old launcher still owns the numeric id
        # and must reclaim it directly before rolling back its record.
        if sid is not None:
            with contextlib.suppress(Exception):
                ava.shell.sessions.kill(sid)
        with contextlib.suppress(Exception):
            coding_session_owner.terminate_generation(
                key,
                generation,
                reason="launch-failed",
            )
        raise

    print(f"ready. name={active.expected_suffix} workspace={workspace}")
    _print_owner(active, adopted=False)
    return 0


def _launch(
    workspace: Path,
    tasks_file: Path | None,
    work_file: Path | None,
    ttl_seconds: float,
    caller_instance: str | None = None,
    impersonation_name: str | None = None,
    brief: str | None = None,
    *,
    relay_resident: bool = True,
    relay_plugin_dir: Path | None = None,
) -> int:
    from shared.external_caller import launch_caller_assignment

    takeover_name: str | None = impersonation_name
    takeover_brief = ""
    if takeover_name is not None:
        if not brief or not brief.strip():
            raise ValueError("a takeover launch needs a non-empty briefing")
        if tasks_file is not None or work_file is not None:
            raise ValueError("a takeover launch reads no task or work file")
        takeover_brief = brief
    elif tasks_file is None or work_file is None:
        raise ValueError("a supervised launch needs its task and work files")

    # Validate before creating files, owner records, or sessions.
    launch_caller_assignment("claude_code", caller_instance)
    _preset_claude_first_run()
    if takeover_name is None:
        assert tasks_file is not None and work_file is not None  # noqa: S101 — checked above
        return _run_supervised_launch(
            workspace, tasks_file, work_file, ttl_seconds, caller_instance
        )
    return _run_takeover_launch(
        workspace,
        takeover_name,
        takeover_brief,
        ttl_seconds,
        caller_instance,
        relay_resident=relay_resident,
        relay_plugin_dir=relay_plugin_dir,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-trust a workspace and launch Claude Code in a persistent shell session."
    )
    parser.add_argument(
        "workspace",
        help="Workspace directory for Claude Code. Any directory; no layout is assumed.",
    )
    parser.add_argument(
        "--tasks-file",
        default=None,
        help="Where you append tasks for the agent, absolute or relative to the "
        "workspace (default: tasks.md). Supervised worker mode only.",
    )
    parser.add_argument(
        "--work-file",
        default=None,
        help="Where the agent reports STATUS + log; this is what watch_work.py "
        "polls, absolute or relative to the workspace (default: work.md). "
        "Supervised worker mode only.",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=float,
        default=_DEFAULT_TTL_SECONDS,
        help="Task-adapted hard expiry, up to one day (default: %(default)s).",
    )
    parser.add_argument(
        "--caller-instance",
        default=None,
        help="opt in to v1 external provenance (bounded instance ID); requires target protocol support",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--status",
        action="store_true",
        help="Print the canonical owner record (takeover generations; supervised "
        "launches are not registered).",
    )
    action.add_argument(
        "--cancel-generation",
        metavar="GENERATION",
        help="Stop and terminalize exactly this canonical generation "
        "(takeover generations; supervised launches are not registered).",
    )
    parser.add_argument(
        "--impersonate-self", action="store_true", help="replace the launching Ava agent"
    )
    parser.add_argument("--impersonation-name", help="display name for the takeover session")
    parser.add_argument(
        "--brief",
        default=None,
        help="Takeover briefing text, inlined verbatim into the launch message. "
        "Required with --impersonate-self; a takeover reads no files.",
    )
    relay_action = parser.add_mutually_exclusive_group()
    relay_action.add_argument(
        "--no-relay-resident",
        action="store_true",
        help="Launch the takeover without the session relay plugin; the executor "
        "arms the Claude Monitor relay itself, as before.",
    )
    relay_action.add_argument(
        "--relay-resident-dir",
        default=None,
        help="Override the relay plugin directory loaded into the takeover "
        "(default: the ava-relay directory beside this launcher).",
    )
    args = parser.parse_args()
    if args.impersonate_self:
        from ava._boot import require_agent_id

        require_agent_id()
        if args.status or args.cancel_generation:
            parser.error("--impersonate-self requires a new launch")
        if args.tasks_file is not None or args.work_file is not None:
            parser.error(
                "--tasks-file/--work-file serve the supervised worker mode; a takeover reads no files"
            )
        if args.brief is None or not args.brief.strip():
            parser.error("--impersonate-self requires a non-empty --brief")
    else:
        if args.impersonation_name is not None:
            parser.error("--impersonation-name requires --impersonate-self")
        if args.brief is not None:
            parser.error("--brief requires --impersonate-self")

    workspace = Path(args.workspace).expanduser().resolve()
    if not args.status and not args.cancel_generation:
        workspace = _resolve_dir(args.workspace)
    key = coding_session_owner.canonical_key(workspace, tool="claude")
    if args.status:
        return _status(key)
    if args.cancel_generation:
        return _cancel(key, args.cancel_generation)
    tasks_file = None
    work_file = None
    if not args.impersonate_self:
        tasks_file = _resolve_file(workspace, args.tasks_file or "tasks.md")
        work_file = _resolve_file(workspace, args.work_file or "work.md")
    relay_resident, relay_plugin_dir = _relay_options(parser, args)
    return _launch(
        workspace,
        tasks_file,
        work_file,
        args.ttl_seconds,
        args.caller_instance,
        (args.impersonation_name or workspace.name) if args.impersonate_self else None,
        args.brief,
        relay_resident=relay_resident,
        relay_plugin_dir=relay_plugin_dir,
    )


if __name__ == "__main__":
    sys.exit(main())
