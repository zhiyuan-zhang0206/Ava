"""Generation-owned lifecycle for external coding-tool sessions.

A generation is either *supervised* — a file-driven delegated worker with a
task file, a work file, and an automatic lifecycle supervisor — or a
*file-less takeover* that replaces the launching Ava agent: no files, no
supervisor, and its coding session alone is the liveness signal.

Every launch owns a generation of its own under the key ``(cluster home,
canonical workspace, tool)``; several may share a workspace. A launch first
reclaims its dead siblings (expired, crashed, unsupervised, or owned by a
terminated agent) and leaves live ones alone. Transitions under one key are
serialized by a host-local lock and scoped to an exact generation. Cleanup
stops the recorded PTY before removing private tool state.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import shutil
import stat
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from base.native_process.os_platform import IS_MACOS, file_lock
from base.sessions.coding_session_owner_record import (
    CodingSessionKey,
    CodingSessionOwner,
    InvalidCodingSessionOwnerError,
    canonical_key,
    display_label,
    expected_suffix,
    full_session_name,
    generation_state_dir,
    generations,
    key_digest,
    lock_path,
    read_legacy_unlocked,
    read_unlocked,
    remove_unlocked,
    state_path,
    supervisor_suffix,
    write_unlocked,
)

__all__ = [
    "CodingSessionCleanupError",
    "CodingSessionGenerationChangedError",
    "CodingSessionKey",
    "CodingSessionOwner",
    "CodingSessionSocketError",
    "InvalidCodingSessionOwnerError",
    "attach_supervisor",
    "canonical_key",
    "codex_app_server_socket",
    "full_session_name",
    "generation_state_dir",
    "launch_generation",
    "launch_is_stale",
    "list_generations",
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
OwnerTerminated = Callable[[int], bool]


class CodingSessionCleanupError(RuntimeError):
    """A generation could not prove its PTY and isolated state were reclaimed."""


class CodingSessionGenerationChangedError(RuntimeError):
    """A launcher's generation ended (or was reclaimed) before it could publish."""


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


# A unix socket path must fit ``sockaddr_un.sun_path`` with its terminating NUL:
# 104 bytes on macOS, 108 on Linux.
_SUN_PATH_BYTES = 104 if IS_MACOS else 108
# Short real directories to hold the per-user socket directory. On macOS
# ``/tmp`` is a symlink, and codex refuses a socket directory reached through
# one, so the real ``/private/tmp`` is used.
_SOCKET_BASE = Path("/private/tmp" if IS_MACOS else "/tmp")  # noqa: S108 — the per-user dir below is checked: real, ours, 0700


class CodingSessionSocketError(RuntimeError):
    """No private, short-enough directory is available for an app-server socket."""


def _private_socket_dir() -> Path:
    """``<short tmp>/ava-<uid>``: a real directory this user owns, mode 0700.

    A world-writable parent lets anyone pre-create the name, so a directory
    that is a symlink or belongs to another user is refused rather than used.
    """
    uid = os.getuid()
    directory = _SOCKET_BASE / f"ava-{uid}"
    with contextlib.suppress(FileExistsError):
        directory.mkdir(mode=0o700)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
        raise CodingSessionSocketError(
            f"{directory} is not a directory owned by uid {uid}; refusing to put a "
            "Codex app-server socket there (remove it, or it was created by someone else)"
        )
    if stat.S_IMODE(info.st_mode) & 0o077:
        directory.chmod(0o700)
    return directory


def codex_app_server_socket(key: CodingSessionKey, generation: str) -> Path:
    """Private host-local unix socket for a generation's shared Codex app server.

    The socket lives in a short per-user directory (``/private/tmp/ava-<uid>``
    on macOS, ``/tmp/ava-<uid>`` elsewhere), created 0700 and verified to be
    this user's real directory, so its length no longer depends on the cluster
    home: a home under a long directory path pushed a
    ``<home>/run`` socket past the kernel limit and codex refused to listen.
    The name carries the key digest (cluster, workspace, tool) and the
    generation, so clusters never collide and a dying predecessor can never
    unlink a successor's socket; a crashed generation's stale file is inert.
    A path that would still not fit ``sun_path`` fails here, before any launch.
    """
    name = f"codex-app-server.{key_digest(key)[:12]}-{generation.replace('-', '')[:8]}.sock"
    path = _private_socket_dir() / name
    size = len(os.fsencode(path))
    if size >= _SUN_PATH_BYTES:
        raise CodingSessionSocketError(
            f"Codex app-server socket path {path} is {size} bytes; unix sockets on this "
            f"host take at most {_SUN_PATH_BYTES - 1}"
        )
    return path


def read(key: CodingSessionKey, generation: str) -> CodingSessionOwner:
    """Read one generation's atomic snapshot without taking the transition lock."""
    return read_unlocked(key, generation)


def list_generations(key: CodingSessionKey) -> list[CodingSessionOwner]:
    """Every generation recorded under ``key``, including invalid records."""
    return [read_unlocked(key, generation) for generation in generations(key)]


def _default_list_sessions() -> list[str]:
    from base.sessions.backend import get_shell_backend

    return get_shell_backend().list_sessions()


def _default_session_live(name: str) -> bool:
    from base.sessions.backend import get_shell_backend

    return get_shell_backend().has_session(name)


def _default_terminate_session(name: str) -> bool:
    from base.sessions.backend import get_shell_backend

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


def _reclaimable(
    owner: CodingSessionOwner,
    *,
    now: dt.datetime,
    list_sessions: SessionLister,
    session_live: SessionLiveness,
    owner_terminated: OwnerTerminated,
) -> bool:
    """Whether a sibling generation is over and may be cleaned up by another launch."""
    if owner.status == "terminal":
        return True
    if owner.status not in ("launching", "active"):
        return False  # inactive or invalid: nothing to reclaim, never guess
    if owner.owner_agent_id is not None and owner_terminated(owner.owner_agent_id):
        return True
    if owner.status == "launching":
        return launch_is_stale(owner, now=now) and not any(
            session_live(name) for name in _candidate_sessions(owner, list_sessions)
        )
    if owner.expires_at is None or now >= owner.expires_at:
        return True
    if owner.session_name is None or not session_live(owner.session_name):
        return True
    # A supervised generation lives only while its supervisor does; a file-less
    # takeover has none and lives on its coding session alone.
    return owner.work_file is not None and not (
        owner.supervisor_session_name is not None and session_live(owner.supervisor_session_name)
    )


def _reclaim_siblings_unlocked(
    key: CodingSessionKey,
    *,
    now: dt.datetime,
    list_sessions: SessionLister,
    session_live: SessionLiveness,
    terminate_session: SessionTerminator,
    owner_terminated: OwnerTerminated,
) -> None:
    """Clean up and drop every dead generation under ``key``, the legacy slot included."""
    siblings = [read_unlocked(key, generation) for generation in generations(key)]
    legacy = read_legacy_unlocked(key)
    for owner, generation in [*((o, o.generation) for o in siblings), (legacy, None)]:
        if not _reclaimable(
            owner,
            now=now,
            list_sessions=list_sessions,
            session_live=session_live,
            owner_terminated=owner_terminated,
        ):
            continue
        if owner.status != "terminal":
            _cleanup_unlocked(
                owner,
                list_sessions=list_sessions,
                session_live=session_live,
                terminate_session=terminate_session,
            )
        remove_unlocked(key, generation)


def _never_terminated(_agent_id: int) -> bool:
    return False


def launch_generation(
    key: CodingSessionKey,
    *,
    owner_agent_id: int,
    tasks_file: Path | None,
    work_file: Path | None,
    ttl_seconds: float,
    now: dt.datetime | None = None,
    list_sessions: SessionLister = _default_list_sessions,
    session_live: SessionLiveness = _default_session_live,
    terminate_session: SessionTerminator = _default_terminate_session,
    owner_terminated: OwnerTerminated = _never_terminated,
) -> CodingSessionOwner:
    """Reclaim dead siblings, then publish a fresh ``launching`` generation of our own."""
    if owner_agent_id < 0:
        raise ValueError("owner_agent_id must be non-negative")
    if not 0 < ttl_seconds <= _MAX_TTL_SECONDS:
        raise ValueError("ttl_seconds must be greater than zero and at most one day")
    if (tasks_file is None) != (work_file is None):
        raise ValueError("task and work files are given together, or neither for a takeover")
    timestamp = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    with file_lock(lock_path(key), timeout_s=_LOCK_TIMEOUT_S):
        _reclaim_siblings_unlocked(
            key,
            now=timestamp,
            list_sessions=list_sessions,
            session_live=session_live,
            terminate_session=terminate_session,
            owner_terminated=owner_terminated,
        )
        generation = str(uuid.uuid4())
        owner = CodingSessionOwner(
            key=key,
            status="launching",
            generation=generation,
            owner_agent_id=owner_agent_id,
            display_label=display_label(key.workspace),
            expected_suffix=expected_suffix(key, generation),
            state_dir=generation_state_dir(key, generation),
            tasks_file=tasks_file.expanduser().resolve() if tasks_file is not None else None,
            work_file=work_file.expanduser().resolve() if work_file is not None else None,
            created_at=timestamp,
            expires_at=timestamp + dt.timedelta(seconds=ttl_seconds),
        )
        write_unlocked(owner)
        return owner


def attach_supervisor(
    key: CodingSessionKey,
    generation: str,
    *,
    session_id: int,
    session_name: str,
) -> CodingSessionOwner:
    """CAS-publish the supervisor handle before launching the coding PTY."""
    with file_lock(lock_path(key), timeout_s=_LOCK_TIMEOUT_S):
        current = read_unlocked(key, generation)
        if current.status != "launching":
            raise CodingSessionGenerationChangedError("owner generation changed before supervision")
        if current.work_file is None:
            raise RuntimeError("cannot attach a supervisor to a file-less takeover generation")
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
        current = read_unlocked(key, generation)
        if current.status != "launching":
            raise CodingSessionGenerationChangedError("owner generation changed during launch")
        if current.work_file is not None and (
            current.supervisor_session_id is None or current.supervisor_session_name is None
        ):
            raise RuntimeError("cannot activate a supervised coding session without its supervisor")
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
    """Stop and terminalize exactly ``generation``; one with no record returns False."""
    if not reason:
        raise ValueError("terminal reason must be non-empty")
    with file_lock(lock_path(key), timeout_s=_LOCK_TIMEOUT_S):
        current = read_unlocked(key, generation)
        if current.status == "invalid":
            raise InvalidCodingSessionOwnerError(
                f"invalid owner record {state_path(key, generation)}: {current.error}"
            )
        if current.status == "inactive":
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
