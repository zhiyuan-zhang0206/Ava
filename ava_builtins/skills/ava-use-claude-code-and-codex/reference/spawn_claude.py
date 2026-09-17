#!/usr/bin/env python3
"""Create, adopt, inspect, or stop the canonical Claude Code workspace generation.

The active identity is ``(cluster, canonical workspace, claude)``. A supervised
worker (the default) keeps the two-file collaboration — a task file, a work
file, and a session the launcher's owner follows with ``watch_work.py``. A
takeover (``--impersonate-self``) runs file- and supervisor-less with its
briefing inlined in the launch message: its Claude Monitor relay is started by
the executor itself, from the briefing and the guide. A concurrent or
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
reads and writes no files and starts no supervisor. ``--status`` /
``--cancel-generation`` inspect or stop the record.

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
import shlex
import sys
import time
from pathlib import Path

import ava
from shared import coding_session_owner
from shared.agents import AgentNotFound, AgentStatus

_DEFAULT_TTL_SECONDS = 24 * 3600


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
    # This script lives in skills/ava-use-claude-code-and-codex/reference/;
    # the collaboration protocol is in the same directory.
    return Path(__file__).resolve().parent / "collaboration_protocol.md"


def _wait_for_ready(sid: int, timeout: float = 30.0) -> None:
    """Poll ``ava.shell.sessions.capture`` until the tool has rendered its UI.

    Claude Code startup takes 5-10 seconds.  A fixed ``time.sleep(3)`` often
    sends the contract message before the TUI is ready, causing it to be lost.
    """
    print(f"waiting for session {sid} to be ready (polling capture)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        output = ava.shell.sessions.capture(sid, scrollback=False)
        if len(output) > 50:
            time.sleep(2)  # brief stability pause
            print("  -> ready")
            return
        time.sleep(1)
    print(f"  -> timeout after {timeout:.0f} s, sending anyway")


def _verify_start_receipt(sid: int, timeout: float = 30.0) -> None:
    """The takeover bootstrap must visibly land, not vanish into the composer.

    A live session is not receipt: a message parked in the composer leaves an
    executor that never learned it replaced the agent (F1 class — do not infer
    receipt from a zero exit code). Receipt = the capture shows the bootstrap's
    opening line. If it does not appear within ``timeout``, press Enter once (a
    stale composer entry submits there) and re-check. Kept loud but not fatal:
    the session may still be rendering; the operator sees the warning.
    """
    print("verifying the takeover bootstrap reached the session...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "take over Ava agent" in ava.shell.sessions.capture(sid):
            print("  -> start-receipt=visible")
            return
        time.sleep(2)
    print("  -> not visible within window; sending Enter once")
    ava.shell.sessions.send_keys(sid, "Enter")
    time.sleep(5)
    if "take over Ava agent" in ava.shell.sessions.capture(sid):
        print("  -> start-receipt=visible after Enter retry")
        return
    print(
        "  -> WARNING: start-receipt=not-visible; the takeover bootstrap may not have "
        "reached the executor. Check the session before relying on it."
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


def _claude_command(workspace: Path, caller_instance: str | None = None) -> str:
    from shared.external_caller import launch_caller_assignment

    return (
        f"cd {shlex.quote(workspace.as_posix())} && "
        "unset ANTHROPIC_API_KEY && "
        f"{launch_caller_assignment('claude_code', caller_instance)}"
        "claude --dangerously-skip-permissions"
    )


def _takeover_bootstrap_message(agent_id: int, name: str, brief: str) -> str:
    """Inline the briefing; a takeover reads no task or work file."""
    from ava._impersonation_launch import bootstrap_message

    guide = (
        Path(__file__).resolve().parents[4]
        / ".agents"
        / "skills"
        / "impersonator-guide"
        / "SKILL.md"
    )
    return bootstrap_message(agent_id, name, "claude", brief, guide)


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
    ava.shell.sessions.send(sid, _claude_command(workspace, caller_instance))
    print(f"+ persistent shell session: {sid} ({session_name})")

    _wait_for_ready(sid)

    contract = _contract_path()
    msg = (
        f"Read the collaboration contract at {contract} and follow it. "
        f"Your workspace is {workspace}. "
        f"Your task file (read-only for you) is {tasks_file}. "
        f"Your work file (yours to write, STATUS + log) is {work_file}. "
        "Now read the task file and start working."
    )
    ava.shell.sessions.send(sid, msg)

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
) -> int:
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
    _pretrust(workspace)
    sid: int | None = None
    try:
        sid = ava.shell.sessions.new(name=expected_suffix, ttl=ttl_seconds)
        full_name = coding_session_owner.full_session_name(owner_agent_id, sid, expected_suffix)
        active = coding_session_owner.publish_active(
            key,
            generation,
            session_id=sid,
            session_name=full_name,
        )
        ava.shell.sessions.send(sid, _claude_command(workspace, caller_instance))
        _wait_for_ready(sid)
        message = _takeover_bootstrap_message(owner_agent_id, takeover_name, takeover_brief)
        ava.shell.sessions.send(sid, message)
        _verify_start_receipt(sid)
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
    if takeover_name is None:
        assert tasks_file is not None and work_file is not None  # noqa: S101 — checked above
        return _run_supervised_launch(
            workspace, tasks_file, work_file, ttl_seconds, caller_instance
        )
    return _run_takeover_launch(
        workspace, takeover_name, takeover_brief, ttl_seconds, caller_instance
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
    action.add_argument("--status", action="store_true", help="Print the canonical owner record.")
    action.add_argument(
        "--cancel-generation",
        metavar="GENERATION",
        help="Stop and terminalize exactly this canonical generation.",
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
    return _launch(
        workspace,
        tasks_file,
        work_file,
        args.ttl_seconds,
        args.caller_instance,
        (args.impersonation_name or workspace.name) if args.impersonate_self else None,
        args.brief,
    )


if __name__ == "__main__":
    sys.exit(main())
