"""Crash-safe local ownership for a detached host updater.

The parent pauses a host before its detached updater can write the Postgres
updater lease. A session-record write can fail after fork/Popen, so an exception
cannot prove that no child exists. This small host-local marker bridges that
gap and then stays for the updater's complete lifetime: the parent publishes a
``pending`` generation before pause, and the child atomically turns that exact
generation into ``running`` with its own PID + process birth time before any
checkout or service mutation.

It is not the deployment UI marker and Gate never reads it. Pending expiry only
opens a recovery attempt; it never proves a running child dead. Recovery may
clear a running generation only when PID + birth time prove that owner gone.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never, cast

import psutil

import shared.paths
from shared.deploy_timing import NO_PROGRESS_TIMEOUT_S
from shared.platform import file_lock

_LOCK_TIMEOUT_S = 5.0
_BOOTSTRAP_RECOVERY_VERSION = 1
_MAX_BOOTSTRAP_RECOVERY_BYTES = 256 * 1024
_log = logging.getLogger("shared.updater_handoff")


class UpdaterHandoffActive(RuntimeError):  # noqa: N818 — active state verdict
    """A fresh or unreadable handoff already owns the spawn gap."""


class BootstrapRecoveryInvalidError(RuntimeError):
    """Retained compensating evidence is present but cannot be authenticated."""


@dataclass(frozen=True)
class UpdaterHandoffSnapshot:
    status: Literal["inactive", "pending", "running", "invalid"]
    generation: str | None = None
    expected_session: str | None = None
    created_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    expired: bool = False
    owner_pid: int | None = None
    owner_create_time: float | None = None


def _invalid(message: str) -> Never:
    raise ValueError(message)


def new_generation() -> str:
    """Mint an opaque token for one updater handoff generation."""
    return str(uuid.uuid4())


def state_path() -> Path:
    return shared.paths.run_dir() / "updater-handoff.json"


def bootstrap_state_path() -> Path:
    """Versioned compensating evidence, separate from ordinary spawn ownership."""
    return shared.paths.run_dir() / "updater-bootstrap-recovery.json"


def lock_path() -> Path:
    return shared.paths.run_dir() / "updater-handoff.lock"


def _timestamp(value: object, field: str) -> dt.datetime:
    if not isinstance(value, str):
        _invalid(f"{field} must be an RFC3339 string")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must carry a timezone")
    return parsed.astimezone(dt.UTC)


def _read_unlocked(path: Path, *, now: dt.datetime | None = None) -> UpdaterHandoffSnapshot:
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            _invalid("handoff root must be an object")
        raw = cast("dict[str, object]", raw)
        generation = raw["generation"]
        expected_session = raw["expected_session"]
        if not isinstance(generation, str) or not generation:
            _invalid("generation must be a non-empty string")
        if not isinstance(expected_session, str) or not expected_session:
            _invalid("expected_session must be a non-empty string")
        phase = raw["phase"]
        if phase not in ("pending", "running"):
            _invalid("phase must be pending or running")
        created_at = _timestamp(raw["created_at"], "created_at")
        expires_at = _timestamp(raw["expires_at"], "expires_at")
        owner_pid_raw = raw.get("owner_pid")
        owner_create_time_raw = raw.get("owner_create_time")
        if phase == "pending":
            if owner_pid_raw is not None or owner_create_time_raw is not None:
                _invalid("pending handoff must not carry an owner identity")
            owner_pid = None
            owner_create_time = None
        else:
            if not isinstance(owner_pid_raw, int) or owner_pid_raw <= 0:
                _invalid("running handoff owner_pid must be a positive integer")
            if not isinstance(owner_create_time_raw, (int, float)):
                _invalid("running handoff owner_create_time must be numeric")
            owner_pid = owner_pid_raw
            owner_create_time = float(owner_create_time_raw)
    except FileNotFoundError:
        return UpdaterHandoffSnapshot(status="inactive")
    except (KeyError, OSError, TypeError, ValueError) as exc:
        _log.warning("[updater-handoff] invalid %s: %s", path, exc)
        return UpdaterHandoffSnapshot(status="invalid")
    expired = expires_at <= (now or dt.datetime.now(dt.UTC))
    return UpdaterHandoffSnapshot(
        status=phase,
        generation=generation,
        expected_session=expected_session,
        created_at=created_at,
        expires_at=expires_at,
        expired=expired,
        owner_pid=owner_pid,
        owner_create_time=owner_create_time,
    )


def read(*, now: dt.datetime | None = None) -> UpdaterHandoffSnapshot:
    """Read a complete snapshot; malformed content is conservatively invalid."""
    return _read_unlocked(state_path(), now=now)


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(dir=path.parent, prefix=".updater-handoff-", suffix=".tmp")
    if os.name != "nt":
        os.fchmod(fd, 0o600)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)  # noqa: PTH105 — explicit atomic replace injection seam
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        _fsync_parent(path)
    except OSError:
        _log.warning("[updater-handoff] directory fsync failed after commit", exc_info=True)


def _bounded_bytes(path: Path, *, limit: int) -> bytes:
    """Read one identity-stable regular file without following a substituted link."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise BootstrapRecoveryInvalidError("bootstrap recovery is not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise BootstrapRecoveryInvalidError("bootstrap recovery changed while opening")
            body = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except (OSError, ValueError) as exc:
        raise BootstrapRecoveryInvalidError("bootstrap recovery cannot be read safely") from exc
    if (
        len(body) > limit
        or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise BootstrapRecoveryInvalidError("bootstrap recovery changed while reading")
    return body


def _read_bootstrap_unlocked() -> dict[str, object] | None:
    path = bootstrap_state_path()
    try:
        raw: object = json.loads(_bounded_bytes(path, limit=_MAX_BOOTSTRAP_RECOVERY_BYTES))
        if not isinstance(raw, dict):
            raise BootstrapRecoveryInvalidError("bootstrap recovery envelope is malformed")
        envelope = cast("dict[str, object]", raw)
        if set(envelope) != {"version", "generation", "journal"}:
            raise BootstrapRecoveryInvalidError("bootstrap recovery envelope is malformed")
        if envelope["version"] != _BOOTSTRAP_RECOVERY_VERSION:
            raise BootstrapRecoveryInvalidError("bootstrap recovery version is unsupported")
        if not isinstance(envelope["generation"], str) or not envelope["generation"]:
            raise BootstrapRecoveryInvalidError("bootstrap recovery generation is malformed")
        if not isinstance(envelope["journal"], dict):
            raise BootstrapRecoveryInvalidError("bootstrap recovery journal is malformed")
        return envelope
    except FileNotFoundError:
        return None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BootstrapRecoveryInvalidError("bootstrap recovery envelope is malformed") from exc


def read_bootstrap_recovery() -> dict[str, object] | None:
    """Read the bounded, versioned compensation envelope without discarding errors."""
    return _read_bootstrap_unlocked()


def _write_bootstrap_unlocked(generation: str, journal: dict[str, object]) -> None:
    payload: dict[str, object] = {
        "version": _BOOTSTRAP_RECOVERY_VERSION,
        "generation": generation,
        "journal": journal,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    if len(encoded) > _MAX_BOOTSTRAP_RECOVERY_BYTES:
        raise BootstrapRecoveryInvalidError("bootstrap recovery exceeds its evidence budget")
    _write_atomic(bootstrap_state_path(), payload)


def write_bootstrap_recovery(generation: str, journal: dict[str, object]) -> None:
    """Replace compensation evidence only for this process's exact running claim."""
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        current = _read_unlocked(state_path())
        process = psutil.Process()
        if (
            current.status != "running"
            or current.generation != generation
            or current.owner_pid != process.pid
            or current.owner_create_time != process.create_time()
        ):
            raise BootstrapRecoveryInvalidError("bootstrap writer lost exact handoff ownership")
        existing = _read_bootstrap_unlocked()
        if existing is not None and existing["generation"] != generation:
            raise BootstrapRecoveryInvalidError("bootstrap recovery generation changed")
        _write_bootstrap_unlocked(generation, journal)


def begin(
    *,
    expected_session: str,
    generation: str | None = None,
    ttl_s: float = NO_PROGRESS_TIMEOUT_S,
) -> UpdaterHandoffSnapshot:
    """Publish a pending child before the parent pauses the host."""
    generation = generation or new_generation()
    if not generation or not expected_session:
        raise ValueError("generation and expected_session must be non-empty")
    path = state_path()
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        if bootstrap_state_path().exists():
            raise UpdaterHandoffActive("restricted bootstrap recovery requires checked resume")
        current = _read_unlocked(path)
        reclaimable = (current.status == "pending" and current.expired) or (
            current.status == "running" and not owner_is_live(current)
        )
        if current.status == "invalid" or (
            current.status in ("pending", "running") and not reclaimable
        ):
            raise UpdaterHandoffActive(
                f"updater handoff {current.generation!r} is already active or unreadable"
            )
        now = dt.datetime.now(dt.UTC)
        payload: dict[str, object] = {
            "phase": "pending",
            "generation": generation,
            "expected_session": expected_session,
            "created_at": now.isoformat(),
            "expires_at": (now + dt.timedelta(seconds=ttl_s)).isoformat(),
        }
        _write_atomic(path, payload)
        return _read_unlocked(path, now=now)


def begin_bootstrap_after_dead_owner(
    predecessor: UpdaterHandoffSnapshot,
    *,
    expected_session: str,
) -> UpdaterHandoffSnapshot | None:
    """Atomically replace the exact dead predecessor with this bootstrap updater."""
    if not expected_session:
        raise ValueError("expected_session must be non-empty")
    path = state_path()
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        current = _read_unlocked(path)
        if (
            current != predecessor
            or current.status != "running"
            or owner_is_live(current)
            or bootstrap_state_path().exists()
        ):
            return None
        now = dt.datetime.now(dt.UTC)
        process = psutil.Process()
        _write_atomic(
            path,
            {
                "phase": "running",
                "generation": new_generation(),
                "expected_session": expected_session,
                "created_at": now.isoformat(),
                "expires_at": (now + dt.timedelta(seconds=NO_PROGRESS_TIMEOUT_S)).isoformat(),
                "owner_pid": process.pid,
                "owner_create_time": process.create_time(),
            },
        )
        return _read_unlocked(path, now=now)


def claim_running(
    generation: str,
    *,
    expected_session: str,
    owner_pid: int | None = None,
) -> bool:
    """CAS-claim one fresh pending generation for the calling updater.

    ``owner_pid`` defaults to this process. The Windows shell helper supplies
    its root ``cmd.exe`` parent instead, because that process synchronously
    owns the complete native update chain. Process birth time is read locally;
    callers cannot inject an unverifiable identity.
    """
    owner_pid = os.getpid() if owner_pid is None else owner_pid
    try:
        owner_create_time = psutil.Process(owner_pid).create_time()
    except (psutil.Error, OSError):
        return False
    path = state_path()
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        current = _read_unlocked(path)
        if (
            current.status != "pending"
            or current.expired
            or current.generation != generation
            or current.expected_session != expected_session
        ):
            return False
        if current.created_at is None or current.expires_at is None:
            return False
        _write_atomic(
            path,
            {
                "phase": "running",
                "generation": generation,
                "expected_session": expected_session,
                "created_at": current.created_at.isoformat(),
                "expires_at": current.expires_at.isoformat(),
                "owner_pid": owner_pid,
                "owner_create_time": owner_create_time,
            },
        )
        return True


def owner_is_live(snapshot: UpdaterHandoffSnapshot) -> bool:
    """Whether a running marker's exact process identity may still be alive.

    False is returned only for positive death evidence: the PID is absent or it
    now belongs to a process with a different birth time. Permission/read
    failures are fail-closed and therefore count as live.
    """
    if snapshot.status != "running":
        raise ValueError("owner liveness is defined only for a running handoff")
    if snapshot.owner_pid is None or snapshot.owner_create_time is None:
        raise ValueError("running handoff has no process identity")
    try:
        actual = psutil.Process(snapshot.owner_pid).create_time()
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, psutil.Error, OSError):
        return True
    return abs(actual - snapshot.owner_create_time) <= 2.0


def clear(generation: str) -> bool:
    """CAS-clear only the handoff generation the caller owns."""
    path = state_path()
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        current = _read_unlocked(path)
        if current.generation != generation:
            return False
        try:
            bootstrap = _read_bootstrap_unlocked()
        except BootstrapRecoveryInvalidError:
            return False
        if bootstrap is not None:
            journal = cast("dict[str, object]", bootstrap["journal"])
            if bootstrap["generation"] != generation or journal.get("stage") not in {
                "candidate_ready",
                "recovered",
            }:
                return False
            normal = journal.get("normal_release")
            if normal is not None:
                if not isinstance(normal, dict):
                    return False
                normal = cast("dict[str, object]", normal)
                if normal.get("stage") != "committed":
                    return False
            bootstrap_state_path().unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            _fsync_parent(path)
        return True


def force_clear() -> bool:
    """Clear stale/invalid state after recovery's no-live-owner proof."""
    path = state_path()
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        if bootstrap_state_path().exists():
            return False
        existed = path.exists()
        path.unlink(missing_ok=True)
        if existed:
            with contextlib.suppress(OSError):
                _fsync_parent(path)
        return existed


def resume_bootstrap(generation: str, *, expected_session: str) -> bool:
    """Reclaim this same retained handoff only after exact owner death evidence.

    The updater first validates the persisted request, operation and image
    references. This changes local process ownership, not release authority.
    """
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        path = state_path()
        current = _read_unlocked(path)
        if (
            current.generation != generation
            or current.status != "running"
            or owner_is_live(current)
        ):
            return False
        try:
            recovery = _read_bootstrap_unlocked()
        except BootstrapRecoveryInvalidError:
            return False
        if recovery is None or recovery["generation"] != generation:
            return False
        payload = json.loads(path.read_text())
        payload.update(
            expected_session=expected_session,
            owner_pid=os.getpid(),
            owner_create_time=psutil.Process().create_time(),
        )
        _write_atomic(path, payload)
        return True
