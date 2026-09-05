"""Generation-owned lifecycle for supervised external coding-tool sessions.

The authoritative key is ``(cluster home, canonical workspace, tool)``. Each
transition is serialized by a host-local per-key lock and scoped to an opaque
generation. Cleanup stops the recorded PTY before removing private tool state.
"""

from __future__ import annotations

import datetime as dt
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from shared.coding_session_owner_record import (
    CodingSessionKey,
    CodingSessionOwner,
    InvalidCodingSessionOwnerError,
    canonical_key,
    display_label,
    expected_suffix,
    full_session_name,
    generation_state_dir,
    lock_path,
    read_unlocked,
    state_path,
    supervisor_suffix,
    write_unlocked,
)
from shared.platform import file_lock

__all__ = [
    "CodingSessionClaim",
    "CodingSessionCleanupError",
    "CodingSessionGenerationChangedError",
    "CodingSessionKey",
    "CodingSessionOwner",
    "InvalidCodingSessionOwnerError",
    "attach_supervisor",
    "canonical_key",
    "claim",
    "full_session_name",
    "generation_state_dir",
    "launch_is_stale",
    "publish_active",
    "read",
    "state_path",
    "supervisor_suffix",
    "terminate_generation",
]

_LOCK_TIMEOUT_S = 60.0
_UNPUBLISHED_CLAIM_WINDOW = dt.timedelta(seconds=60)
_MAX_TTL_SECONDS = 86_400.0

SessionLister = Callable[[], list[str]]
SessionLiveness = Callable[[str], bool]
SessionTerminator = Callable[[str], bool]


class CodingSessionCleanupError(RuntimeError):
    """A generation could not prove its PTY and isolated state were reclaimed."""


class CodingSessionGenerationChangedError(RuntimeError):
    """A stale launcher attempted to publish over a replacement generation."""


@dataclass(frozen=True)
class CodingSessionClaim:
    """Atomic launch decision for one canonical key."""

    action: Literal["launch", "adopt", "busy"]
    owner: CodingSessionOwner


def launch_is_stale(
    owner: CodingSessionOwner,
    *,
    now: dt.datetime | None = None,
) -> bool:
    """Return whether a claimant died before publishing its active handle."""
    if owner.status != "launching" or owner.created_at is None:
        return False
    timestamp = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    return timestamp - owner.created_at >= _UNPUBLISHED_CLAIM_WINDOW


def read(key: CodingSessionKey) -> CodingSessionOwner:
    """Read one atomic owner snapshot without taking its transition lock."""
    return read_unlocked(key)


def _default_list_sessions() -> list[str]:
    from shared.session_backend import get_shell_backend

    return get_shell_backend().list_sessions()


def _default_session_live(name: str) -> bool:
    from shared.session_backend import get_shell_backend

    return get_shell_backend().has_session(name)


def _default_terminate_session(name: str) -> bool:
    from shared.session_backend import get_shell_backend

    stopped, _mode = get_shell_backend().kill_session(name, graceful=False, expected=True)
    return stopped


def _candidate_sessions(owner: CodingSessionOwner, list_sessions: SessionLister) -> list[str]:
    names: set[str] = set()
    if owner.session_name:
        names.add(owner.session_name)
    if owner.expected_suffix:
        names.update(name for name in list_sessions() if name.endswith(f"-{owner.expected_suffix}"))
    return sorted(name for name in names if name)


def _remove_generation_state(owner: CodingSessionOwner) -> None:
    if owner.generation is None or owner.state_dir is None:
        return
    expected = generation_state_dir(owner.key, owner.generation)
    if owner.state_dir.resolve() != expected.resolve():
        raise CodingSessionCleanupError("refusing to remove non-canonical generation state")
    if owner.state_dir.is_symlink():
        owner.state_dir.unlink(missing_ok=True)
    elif owner.state_dir.exists():
        shutil.rmtree(owner.state_dir)


def _cleanup_unlocked(
    owner: CodingSessionOwner,
    *,
    list_sessions: SessionLister,
    session_live: SessionLiveness,
    terminate_session: SessionTerminator,
) -> None:
    for name in _candidate_sessions(owner, list_sessions):
        if session_live(name) and not terminate_session(name):
            raise CodingSessionCleanupError(f"could not stop coding session {name!r}")
        if session_live(name):
            raise CodingSessionCleanupError(f"coding session {name!r} remained live after stop")
    try:
        _remove_generation_state(owner)
    except OSError as exc:
        raise CodingSessionCleanupError(
            f"could not remove isolated state {owner.state_dir}: {exc}"
        ) from exc


def claim(
    key: CodingSessionKey,
    *,
    owner_agent_id: int,
    tasks_file: Path,
    work_file: Path,
    ttl_seconds: float,
    now: dt.datetime | None = None,
    list_sessions: SessionLister = _default_list_sessions,
    session_live: SessionLiveness = _default_session_live,
    terminate_session: SessionTerminator = _default_terminate_session,
    terminated_generation: str | None = None,
) -> CodingSessionClaim:
    """Atomically adopt, wait for, or replace one canonical generation."""
    if owner_agent_id < 0:
        raise ValueError("owner_agent_id must be non-negative")
    if not 0 < ttl_seconds <= _MAX_TTL_SECONDS:
        raise ValueError("ttl_seconds must be greater than zero and at most one day")
    timestamp = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    with file_lock(lock_path(key), timeout_s=_LOCK_TIMEOUT_S):
        current = read_unlocked(key)
        if current.status == "invalid":
            raise InvalidCodingSessionOwnerError(
                f"invalid canonical owner {state_path(key)}: {current.error}"
            )
        current_owner_terminated = current.generation == terminated_generation
        if (
            current.status == "active"
            and current.expires_at is not None
            and timestamp < current.expires_at
            and current.session_name is not None
            and session_live(current.session_name)
            and current.supervisor_session_name is not None
            and session_live(current.supervisor_session_name)
            and not current_owner_terminated
        ):
            return CodingSessionClaim(action="adopt", owner=current)
        if current.status == "launching" and not current_owner_terminated:
            if not launch_is_stale(current, now=timestamp):
                return CodingSessionClaim(action="busy", owner=current)
            if any(session_live(name) for name in _candidate_sessions(current, list_sessions)):
                return CodingSessionClaim(action="busy", owner=current)
        if current.status != "inactive":
            _cleanup_unlocked(
                current,
                list_sessions=list_sessions,
                session_live=session_live,
                terminate_session=terminate_session,
            )
        generation = str(uuid.uuid4())
        label = display_label(key.workspace)
        owner = CodingSessionOwner(
            key=key,
            status="launching",
            generation=generation,
            owner_agent_id=owner_agent_id,
            display_label=label,
            expected_suffix=expected_suffix(key, generation),
            state_dir=generation_state_dir(key, generation),
            tasks_file=tasks_file.expanduser().resolve(),
            work_file=work_file.expanduser().resolve(),
            created_at=timestamp,
            expires_at=timestamp + dt.timedelta(seconds=ttl_seconds),
        )
        write_unlocked(owner)
        return CodingSessionClaim(action="launch", owner=owner)


def attach_supervisor(
    key: CodingSessionKey,
    generation: str,
    *,
    session_id: int,
    session_name: str,
) -> CodingSessionOwner:
    """CAS-publish the supervisor handle before launching the coding PTY."""
    with file_lock(lock_path(key), timeout_s=_LOCK_TIMEOUT_S):
        current = read_unlocked(key)
        if current.status != "launching" or current.generation != generation:
            raise CodingSessionGenerationChangedError("owner generation changed before supervision")
        if current.owner_agent_id is None:
            raise RuntimeError("launching owner has no agent identity")
        if current.generation is None or session_name != full_session_name(
            current.owner_agent_id,
            session_id,
            supervisor_suffix(current.key, current.generation),
        ):
            raise ValueError("supervisor full name does not match its owner, id, and generation")
        updated = replace(
            current,
            supervisor_session_id=session_id,
            supervisor_session_name=session_name,
        )
        write_unlocked(updated)
        return updated


def publish_active(
    key: CodingSessionKey,
    generation: str,
    *,
    session_id: int,
    session_name: str,
) -> CodingSessionOwner:
    """CAS-publish the ready PTY handle for one launching generation."""
    with file_lock(lock_path(key), timeout_s=_LOCK_TIMEOUT_S):
        current = read_unlocked(key)
        if current.status != "launching" or current.generation != generation:
            raise CodingSessionGenerationChangedError("owner generation changed during launch")
        if current.supervisor_session_id is None or current.supervisor_session_name is None:
            raise RuntimeError("cannot activate a coding session without its supervisor")
        if current.owner_agent_id is None:
            raise RuntimeError("launching owner has no agent identity")
        if current.expected_suffix is None or session_name != full_session_name(
            current.owner_agent_id,
            session_id,
            current.expected_suffix,
        ):
            raise ValueError("session name does not match this generation's owner and id")
        updated = replace(
            current,
            status="active",
            session_id=session_id,
            session_name=session_name,
        )
        write_unlocked(updated)
        return updated


def terminate_generation(
    key: CodingSessionKey,
    generation: str,
    *,
    reason: str,
    now: dt.datetime | None = None,
    list_sessions: SessionLister = _default_list_sessions,
    session_live: SessionLiveness = _default_session_live,
    terminate_session: SessionTerminator = _default_terminate_session,
) -> bool:
    """Stop and terminalize exactly ``generation``; stale callers do nothing."""
    if not reason:
        raise ValueError("terminal reason must be non-empty")
    with file_lock(lock_path(key), timeout_s=_LOCK_TIMEOUT_S):
        current = read_unlocked(key)
        if current.status == "invalid":
            raise InvalidCodingSessionOwnerError(
                f"invalid canonical owner {state_path(key)}: {current.error}"
            )
        if current.generation != generation or current.status == "inactive":
            return False
        if current.status == "terminal":
            return True
        _cleanup_unlocked(
            current,
            list_sessions=list_sessions,
            session_live=session_live,
            terminate_session=terminate_session,
        )
        terminal = replace(
            current,
            status="terminal",
            terminalized_at=(now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC),
            terminal_reason=reason,
        )
        write_unlocked(terminal)
        return True
