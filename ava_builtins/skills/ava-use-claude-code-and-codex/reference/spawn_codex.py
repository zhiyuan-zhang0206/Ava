#!/usr/bin/env python3
"""Pre-trust a workspace directory and launch OpenAI Codex in a persistent shell session.

Usage::

    .venv/bin/python spawn_codex.py <workspace-dir>
    .venv/bin/python spawn_codex.py <workspace-dir> --work-file notes/progress.md

Steps:
1. Creates the task file / work file if absent (parent dirs included).
2. Pre-trusts the directory in ``~/.codex/config.toml`` so no trust prompt blocks the session.
3. Creates a persistent shell session via ``ava.shell.sessions.new(name=...)`` named
   ``codex-<dirname>`` and launches ``codex --dangerously-bypass-approvals-and-sandbox``.
4. Polls ``ava.shell.sessions.capture`` until Codex has rendered its UI, then
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
    config_path = Path.home() / ".codex" / "config.toml"
    ws_key = workspace.as_posix()
    section_header = f'[projects."{ws_key}"]'

    try:
        existing = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""

    if section_header in existing:
        print(f"(already trusted: {ws_key})")
        return

    new_section = f'\n{section_header}\ntrust_level = "trusted"\n'
    config_path.write_text(existing.rstrip("\n") + new_section, encoding="utf-8")
    print(f"+ trusted: {ws_key}")


def _session_exists(name: str) -> bool:
    """Check if a persistent shell session with `name` already exists."""
    return any(existing_name == name for existing_name in ava.shell.sessions.list().values())


def _contract_path() -> Path:
    return Path(__file__).resolve().parent / "collaboration_protocol.md"


def _wait_for_ready(sid: int, timeout: float = 30.0) -> None:
    """Poll ``ava.shell.sessions.capture`` until the tool has rendered its UI.

    Codex startup takes 5-10 seconds.  A fixed ``time.sleep(3)`` often
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
    parser = argparse.ArgumentParser(
        description="Pre-trust a workspace and launch OpenAI Codex in a persistent shell session."
    )
    parser.add_argument(
        "workspace",
        help="Workspace directory for Codex. Any directory; no layout is assumed.",
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
    args = parser.parse_args()

    # Agent identity is picked up from the AVA_AGENT_ID environment variable
    # (forwarded by the session machinery and auto-established by ava._boot).
    workspace = _resolve_dir(args.workspace)
    tasks_file = _resolve_file(workspace, args.tasks_file)
    work_file = _resolve_file(workspace, args.work_file)
    _init_file(tasks_file, "")
    _init_file(work_file, "STATUS: WORKING\n\n## Log\n\n")
    _pretrust(workspace)

    session_name = f"codex-{workspace.name}"
    if _session_exists(session_name):
        print(
            f"session '{session_name}' already exists. session id: see `ava.shell.sessions.list()`"
        )
        return 1

    # Create a persistent shell session visible in the Inspect panel.
    sid = ava.shell.sessions.new(name=session_name)

    inner = f"cd {shlex.quote(workspace.as_posix())} && codex --dangerously-bypass-approvals-and-sandbox"
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
