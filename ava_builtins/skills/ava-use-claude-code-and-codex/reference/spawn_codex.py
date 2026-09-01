#!/usr/bin/env python3
"""Create, adopt, inspect, or stop the canonical Codex workspace generation.

The active identity is ``(cluster, canonical workspace, codex)``. A launch
publishes one generation-owned record, starts an automatic lifecycle supervisor,
and gives that generation a private ``CODEX_HOME``. A concurrent or cross-agent
caller adopts the live record instead of stacking another Codex process.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shlex
import shutil
import sys
import time
from pathlib import Path

import ava
from shared import coding_session_owner
from shared.agents import AgentNotFound, AgentStatus

_DEFAULT_TTL_SECONDS = 4 * 3600
_SUPERVISOR_TTL_PADDING_SECONDS = 300


def _resolve_dir(dir_path: str) -> Path:
    path = Path(dir_path).expanduser().resolve()
    if not path.is_dir():
        print(f"error: {path} is not a directory or does not exist", file=sys.stderr)
        raise SystemExit(1)
    return path


def _resolve_file(workspace: Path, raw: str) -> Path:
    """Absolute path stays as given; a relative one is taken against the workspace."""
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (workspace / path).resolve()


def _init_file(path: Path, initial: str) -> None:
    """Create the file with ``initial`` if absent, including parent directories."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(initial, encoding="utf-8")


def _project_header(workspace: Path) -> str:
    return f"[projects.{json.dumps(workspace.as_posix())}]"


def _seed_codex_home(
    codex_home: Path,
    workspace: Path,
    *,
    source_home: Path | None = None,
) -> None:
    """Seed only immutable launch inputs, never SQLite, sessions, or logs."""
    source = source_home or (Path.home() / ".codex")
    codex_home.mkdir(parents=True, exist_ok=False)
    codex_home.chmod(0o700)
    auth_source = source / "auth.json"
    if auth_source.is_file():
        auth_target = codex_home / "auth.json"
        shutil.copyfile(auth_source, auth_target)
        auth_target.chmod(0o600)

    config_source = source / "config.toml"
    config = config_source.read_text(encoding="utf-8") if config_source.is_file() else ""
    section = _project_header(workspace)
    if section not in config:
        config = config.rstrip("\n") + f'\n\n{section}\ntrust_level = "trusted"\n'
    config_target = codex_home / "config.toml"
    config_target.write_text(config.lstrip("\n"), encoding="utf-8")
    config_target.chmod(0o600)


def _contract_path() -> Path:
    return Path(__file__).resolve().parent / "collaboration_protocol.md"


def _watcher_path() -> Path:
    return Path(__file__).resolve().parent / "watch_work.py"


def _wait_for_ready(sid: int, timeout: float = 30.0) -> None:
    """Wait until Codex renders; the owner remains ``launching`` meanwhile."""
    print(f"waiting for session {sid} to be ready (polling capture)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        output = ava.shell.sessions.capture(sid, scrollback=False)
        if len(output) > 50:
            time.sleep(2)
            print("  -> ready")
            return
        time.sleep(1)
    print(f"  -> timeout after {timeout:.0f} s, sending anyway")


def _owner_terminated(agent_id: int) -> bool:
    try:
        return ava.agents.get_status(agent_id) is AgentStatus.TERMINATED
    except AgentNotFound:
        return True
    except Exception:
        # An unavailable gateway cannot prove an owner dead. The supervisor and
        # expiry retain responsibility; guessing here could kill active work.
        return False


def _supervisor_code(owner: coding_session_owner.CodingSessionOwner) -> str:
    if owner.generation is None or owner.owner_agent_id is None or owner.work_file is None:
        raise RuntimeError("launching owner is missing supervisor inputs")
    return (
        "import os, runpy\n"
        f"os.environ['AVA_AGENT_ID'] = {str(owner.owner_agent_id)!r}\n"
        f"_watch = runpy.run_path({str(_watcher_path())!r})['watch']\n"
        f"_watch({str(owner.work_file)!r}, cluster={owner.key.cluster!r}, "
        f"workspace={owner.key.workspace!r}, generation={owner.generation!r}, "
        f"owner_agent_id={owner.owner_agent_id!r})\n"
    )


def _supervisor_name(owner: coding_session_owner.CodingSessionOwner) -> str:
    if owner.generation is None:
        raise RuntimeError("launching owner has no generation")
    return coding_session_owner.supervisor_suffix(owner.key, owner.generation)


def _launch_supervisor(
    owner: coding_session_owner.CodingSessionOwner, ttl_seconds: float
) -> tuple[int, str]:
    """Launch a quiet PTY supervisor that cannot resurrect a terminated owner."""
    supervisor_ttl = min(ttl_seconds + _SUPERVISOR_TTL_PADDING_SECONDS, 86_400)
    suffix = _supervisor_name(owner)
    session_id = ava.shell.sessions.new(
        name=suffix,
        ttl=supervisor_ttl,
    )
    command = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(_supervisor_code(owner))}"
    try:
        ava.shell.sessions.send(session_id, command)
    except BaseException:
        with contextlib.suppress(Exception):
            ava.shell.sessions.kill(session_id)
        raise
    if owner.owner_agent_id is None:
        raise RuntimeError("launching owner has no agent identity")
    return session_id, coding_session_owner.full_session_name(
        owner.owner_agent_id, session_id, suffix
    )


def _codex_command(owner: coding_session_owner.CodingSessionOwner, workspace: Path) -> str:
    if owner.state_dir is None:
        raise RuntimeError("launching owner has no isolated state directory")
    return (
        f"cd {shlex.quote(workspace.as_posix())} && "
        f"CODEX_HOME={shlex.quote(str(owner.state_dir))} "
        "exec codex --dangerously-bypass-approvals-and-sandbox"
    )


def _bootstrap_message(
    workspace: Path,
    tasks_file: Path,
    work_file: Path,
) -> str:
    """Build the complete durable-state handoff for a fresh Codex process."""
    return (
        f"Read the collaboration contract at {_contract_path()} and follow it. "
        f"Your workspace is {workspace}. "
        f"Your task file (read-only for you) is {tasks_file}. "
        f"Your work file (yours to write, STATUS + log) is {work_file}. "
        "Now read the task file and start working."
    )


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
    if owner.supervisor_session_id is not None:
        print(f"supervisor_session_id={owner.supervisor_session_id}")
    if owner.supervisor_session_name is not None:
        print(f"supervisor_session_name={owner.supervisor_session_name}")
    if owner.state_dir is not None:
        print(f"codex_home={owner.state_dir}")
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
    tasks_file: Path,
    work_file: Path,
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


def _launch(
    workspace: Path,
    tasks_file: Path,
    work_file: Path,
    ttl_seconds: float,
) -> int:
    _init_file(tasks_file, "")
    _init_file(work_file, "STATUS: WORKING\n\n## Log\n\n")
    key = coding_session_owner.canonical_key(workspace, tool="codex")
    claim = _claim_canonical(
        key,
        tasks_file=tasks_file,
        work_file=work_file,
        ttl_seconds=ttl_seconds,
    )
    if claim.action == "adopt":
        _print_owner(claim.owner, adopted=True)
        return 0
    owner = claim.owner
    if (
        owner.generation is None
        or owner.expected_suffix is None
        or owner.state_dir is None
        or owner.owner_agent_id is None
    ):
        raise RuntimeError("new canonical owner is missing launch fields")
    generation = owner.generation
    expected_suffix = owner.expected_suffix
    owner_agent_id = owner.owner_agent_id
    sid: int | None = None
    try:
        _seed_codex_home(owner.state_dir, workspace)
        watcher_id, watcher_name = _launch_supervisor(owner, ttl_seconds)
        owner = coding_session_owner.attach_supervisor(
            key,
            generation,
            session_id=watcher_id,
            session_name=watcher_name,
        )
        sid = ava.shell.sessions.new(name=expected_suffix, ttl=ttl_seconds)
        full_name = coding_session_owner.full_session_name(owner_agent_id, sid, expected_suffix)
        ava.shell.sessions.send(sid, _codex_command(owner, workspace))
        _wait_for_ready(sid)
        ava.shell.sessions.send(sid, _bootstrap_message(workspace, tasks_file, work_file))
        active = coding_session_owner.publish_active(
            key,
            generation,
            session_id=sid,
            session_name=full_name,
        )
    except BaseException:
        # A replacement may own the canonical record by now, so its generation
        # CAS cannot find this unpublished PTY. The old launcher still owns the
        # numeric id and must reclaim it directly before rolling back its record.
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch or adopt the canonical supervised Codex workspace generation."
    )
    parser.add_argument("workspace", help="Canonical workspace directory for Codex.")
    parser.add_argument(
        "--tasks-file",
        default="tasks.md",
        help="Task input file, absolute or relative to the workspace (default: %(default)s).",
    )
    parser.add_argument(
        "--work-file",
        default="work.md",
        help="STATUS and log file, absolute or relative to the workspace (default: %(default)s).",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=float,
        default=_DEFAULT_TTL_SECONDS,
        help="Task-adapted hard expiry, up to one day (default: %(default)s).",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true", help="Print the canonical owner record.")
    action.add_argument(
        "--cancel-generation",
        metavar="GENERATION",
        help="Stop and terminalize exactly this canonical generation.",
    )
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    if not args.status and not args.cancel_generation:
        workspace = _resolve_dir(args.workspace)
    key = coding_session_owner.canonical_key(workspace, tool="codex")
    if args.status:
        return _status(key)
    if args.cancel_generation:
        return _cancel(key, args.cancel_generation)
    return _launch(
        workspace,
        _resolve_file(workspace, args.tasks_file),
        _resolve_file(workspace, args.work_file),
        args.ttl_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
