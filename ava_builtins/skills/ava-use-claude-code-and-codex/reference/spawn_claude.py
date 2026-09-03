#!/usr/bin/env python3
"""Pre-trust a workspace directory and launch Claude Code in a persistent shell session.

Usage::

    .venv/bin/python spawn_claude.py <workspace-dir>
    .venv/bin/python spawn_claude.py <workspace-dir> --work-file notes/progress.md

Steps:
1. Creates the task file / work file if absent (parent dirs included).
2. Pre-trusts the directory in ``~/.claude.json`` so no trust prompt blocks the session.
3. Creates a persistent shell session via ``ava.shell.sessions.new(name=..., ttl=...)`` named
   ``claude-<dirname>`` and launches ``claude --dangerously-skip-permissions``
   (after ``unset ANTHROPIC_API_KEY`` to avoid API-key billing trap).
4. Polls ``ava.shell.sessions.capture`` until Claude Code has rendered its UI, then
   sends the collaboration-contract message — which names both file paths, so the
   coding agent is told where they are rather than assuming a layout.

The workspace is any directory: a git worktree, a scratch folder, a checkout of an
unrelated project. This script imposes no structure on it beyond the two files,
and both of those are relocatable via the flags below.

Output: ``session_id``, ``tasks_file``, ``work_file`` — one ``key=value`` per line.
Feed ``work_file`` straight into ``watch_work.py``'s ``WORK_FILE``; do not
reconstruct the path from a convention. Interact via ``ava.shell.sessions.send`` /
``ava.shell.sessions.send_keys`` / ``ava.shell.sessions.capture`` /
``ava.shell.sessions.kill``.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

import ava


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


def main() -> int:
    from shared.external_caller import launch_caller_assignment

    parser = argparse.ArgumentParser(
        description="Pre-trust a workspace and launch Claude Code in a persistent shell session."
    )
    parser.add_argument(
        "workspace",
        help="Workspace directory for Claude Code. Any directory; no layout is assumed.",
    )
    parser.add_argument(
        "--tasks-file",
        default="tasks.md",
        help="Where you append tasks for the agent. Absolute, or relative to the "
        "workspace (default: %(default)s).",
    )
    parser.add_argument(
        "--work-file",
        default="work.md",
        help="Where the agent reports STATUS + log; this is what watch_work.py "
        "polls. Absolute, or relative to the workspace (default: %(default)s).",
    )
    parser.add_argument(
        "--caller-instance",
        default=None,
        help="opt in to v1 external provenance (bounded instance ID); requires target protocol support",
    )
    args = parser.parse_args()
    # Validate before creating files or sessions. This assignment belongs only
    # to the external process, not the Ava-owned launcher/supervisor.
    caller_assignment = launch_caller_assignment("claude_code", args.caller_instance)

    # Agent identity is picked up from the AVA_AGENT_ID environment variable
    # (forwarded by the session machinery and auto-established by ava._boot).
    workspace = _resolve_dir(args.workspace)
    tasks_file = _resolve_file(workspace, args.tasks_file)
    work_file = _resolve_file(workspace, args.work_file)
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
    # mandatory (2026-08-27 ruling); 24h is a generous cap for a coding session
    # — the supervisor re-spawns the script if a session is ever reclaimed.
    sid = ava.shell.sessions.new(name=session_name, ttl=24 * 3600)

    inner = f"cd {shlex.quote(workspace.as_posix())} && unset ANTHROPIC_API_KEY && {caller_assignment}claude --dangerously-skip-permissions"
    ava.shell.sessions.send(sid, inner)
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


if __name__ == "__main__":
    sys.exit(main())
