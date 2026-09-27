"""Launch plumbing both coding-tool launchers share: workspace paths, the
canonical owner claim, and where the impersonator guide lives."""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import ava
from shared import coding_session_owner
from shared.agents import AgentNotFound, AgentStatus


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


def impersonator_guide(reference_dir: Path) -> Path:
    """The takeover executor's manual, from the calling skill's reference directory.

    The skill lives at ``<repo>/ava_builtins/skills/<skill>/reference``; the
    guide is the repository's own ``impersonator-guide`` skill.
    """
    return reference_dir.parents[3] / ".agents" / "skills" / "impersonator-guide" / "SKILL.md"


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
    except Exception:
        # An unavailable gateway cannot prove an owner dead. The supervisor and
        # expiry retain responsibility; guessing here could kill active work.
        return False


def claim_canonical(
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
        and owner_terminated(previous.owner_agent_id)
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


OwnerPrinter = Callable[[coding_session_owner.CodingSessionOwner], None]


def cancel(
    key: coding_session_owner.CodingSessionKey, generation: str, print_owner: OwnerPrinter
) -> int:
    """Stop and terminalize exactly ``generation``; a stale token is refused."""
    stopped = coding_session_owner.terminate_generation(key, generation, reason="explicit-cancel")
    if not stopped:
        print("cancel refused: generation is not the current canonical owner", file=sys.stderr)
        return 1
    print_owner(coding_session_owner.read(key))
    return 0


def status(key: coding_session_owner.CodingSessionKey, print_owner: OwnerPrinter) -> int:
    """Print the canonical owner record; exit 1 when it is invalid."""
    owner = coding_session_owner.read(key)
    print_owner(owner)
    if owner.error:
        print(f"error={owner.error}", file=sys.stderr)
    return 1 if owner.status == "invalid" else 0
