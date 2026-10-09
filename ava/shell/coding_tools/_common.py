"""Launch plumbing both coding-tool launchers share: workspace paths, a new
owner generation, owner-record status and cancel, and where the impersonator
guide lives."""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

import ava
from base.agents import AgentNotFound, AgentStatus, GatewayUnavailable
from base.log import logger
from base.sessions import coding_session_owner
from base.sessions.coding_session_owner_record import CodingSessionStatus


def resolve_dir(dir_path: str) -> Path:
    """The workspace directory, resolved; exit 1 when it does not exist."""
    path = Path(dir_path).expanduser().resolve()
    if not path.is_dir():
        print(f"error: {path} is not a directory or does not exist", file=sys.stderr)
        raise SystemExit(1)
    return path


def resolve_file(workspace: Path, raw: str) -> Path:
    """Absolute path stays as given; a relative one is taken against the workspace."""
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (workspace / path).resolve()


def session_uuid(value: str) -> str:
    """Parse a ``--resume`` argument: the tool session id a launch printed (a UUID)."""
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a session id; pass the UUID the launch printed"
        ) from exc


def init_file(path: Path, initial: str) -> None:
    """Create the file with ``initial`` if absent, including parent directories."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(initial, encoding="utf-8")


def worker_bootstrap(
    contract: Path,
    workspace: Path,
    tasks_file: Path,
    work_file: Path,
    *,
    resumed: bool = False,
) -> str:
    """The supervised worker's first message: the contract and both file paths.

    A resumed session (``resumed``) is told it was interrupted and continues
    from its own history instead of starting over.
    """
    interrupted = (
        "This session was interrupted (its shell was closed) and has been resumed with "
        "its history. "
        if resumed
        else ""
    )
    return (
        f"{interrupted}Read the collaboration contract at {contract} and follow it. "
        f"Your workspace is {workspace}. "
        f"Your task file (read-only for you) is {tasks_file}. "
        f"Your work file (yours to write, STATUS + log) is {work_file}. "
        + (
            "Re-read the task file and your work file, then continue from where you stopped."
            if resumed
            else "Now read the task file and start working."
        )
    )


def owner_terminated(agent_id: int) -> bool:
    try:
        return ava.agents.get_status(agent_id) is AgentStatus.TERMINATED
    except AgentNotFound:
        return True
    except GatewayUnavailable:
        # An unavailable gateway cannot prove an owner dead. The supervisor and
        # expiry retain responsibility; guessing here could kill active work.
        logger.opt(exception=True).warning(
            "gateway unavailable while checking whether owner agent {} terminated; "
            "treating the owner as alive",
            agent_id,
        )
        return False


def kill_session_after_failed_launch(session_id: int) -> None:
    """Reclaim the PTY of a launch that is already failing; the launch's own error stays primary."""
    try:
        ava.shell.sessions.kill(session_id)
    except Exception:
        logger.opt(exception=True).warning(
            "reclaiming shell session {} after a failed coding-session launch failed; "
            "it stays until its TTL expires",
            session_id,
        )


def terminate_generation_after_failed_launch(
    key: coding_session_owner.CodingSessionKey, generation: str
) -> None:
    """Roll back the record of a launch that is already failing; the launch's own error stays primary."""
    try:
        coding_session_owner.terminate_generation(key, generation, reason="launch-failed")
    except Exception:
        logger.opt(exception=True).warning(
            "rolling back coding-session generation {} after a failed launch failed; "
            "its record stays until the sweep reclaims it",
            generation,
        )


def new_generation(
    key: coding_session_owner.CodingSessionKey,
    *,
    tasks_file: Path | None,
    work_file: Path | None,
    ttl_seconds: float,
) -> coding_session_owner.CodingSessionOwner:
    """A fresh generation of our own; dead siblings under ``key`` are reclaimed first."""
    return coding_session_owner.launch_generation(
        key,
        owner_agent_id=ava.self.AGENT_ID,
        tasks_file=tasks_file,
        work_file=work_file,
        ttl_seconds=ttl_seconds,
        owner_terminated=owner_terminated,
    )


OwnerPrinter = Callable[[coding_session_owner.CodingSessionOwner], None]


def cancel(
    key: coding_session_owner.CodingSessionKey, generation: str, print_owner: OwnerPrinter
) -> int:
    """Stop and terminalize exactly ``generation``; a stale token is refused."""
    stopped = coding_session_owner.terminate_generation(key, generation, reason="explicit-cancel")
    if not stopped:
        print(f"cancel refused: no generation {generation} is recorded here", file=sys.stderr)
        return 1
    print_owner(coding_session_owner.read(key, generation))
    return 0


def status(key: coding_session_owner.CodingSessionKey, print_owner: OwnerPrinter) -> int:
    """Print every generation recorded for the workspace; exit 1 when one is invalid."""
    owners = coding_session_owner.list_generations(key)
    if not owners:
        print("status=inactive")
    for index, owner in enumerate(owners):
        if index:
            print()
        print_owner(owner)
        if owner.error:
            print(f"error={owner.error}", file=sys.stderr)
    return 1 if any(owner.status == CodingSessionStatus.INVALID for owner in owners) else 0
