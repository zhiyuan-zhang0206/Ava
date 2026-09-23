"""Persistent UI ownership for a whole-cluster rollout/restart.

The always-up gate is the only owner of maintenance-page rendering.  This
module gives it one durable fact to project: while a whole-cluster
orchestration is active, ``$AVA_HOME/deploy-state.json`` contains one versioned
generation with a stable ``started_at``; completion removes the file.

The file is intentionally not host posture.  Host pause/converge state remains
in ``host_deploy_state`` for the control plane.  A local ``ava start`` can make
the gateway answer before Phase B finishes, so letting start/unpause clear the
UI marker produced the observed Down -> Updating -> App flicker.

Every mutation holds one sibling advisory lock.  Atomic replace prevents a
reader from seeing half JSON; the lock prevents a late phase/clear from one
generation overwriting or deleting a newer generation.  Readers do not lock:
same-directory fsync + replace means they see either complete old state or
complete new state.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never, cast

import shared.paths
from shared.platform import LockTimeoutError, file_lock

SCHEMA_VERSION = 2
STATE_UPDATING = "updating"
_VALID_KINDS = ("rollout", "restart")
UiUpdateKind = Literal["rollout", "restart"]
_LOCK_TIMEOUT_S = 5.0
_RESOURCE_LOCK_TIMEOUT_S = 30.0

_log = logging.getLogger("shared.ui_update_state")
_warning_lock = threading.Lock()
_last_warning_at = [0.0]
_WARNING_INTERVAL_S = 60.0


class UiUpdateAlreadyActive(RuntimeError):  # noqa: N818 — active state verdict
    """A valid active generation already owns the maintenance surface."""


@dataclass(frozen=True)
class UiUpdateSnapshot:
    """One exhaustive projection of the marker file.

    ``status`` is deliberately three-valued.  Missing is inactive;
    a fully valid active marker is updating; corrupt or unknown content is
    invalid.  Invalid is never guessed to mean updating.
    """

    status: Literal["inactive", "updating", "invalid"]
    schema_version: int | None = None
    generation: str | None = None
    kind: UiUpdateKind | None = None
    started_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    phase: str | None = None
    origin: str | None = None
    error: str | None = None


def state_path() -> Path:
    """The gate-visible marker under this cluster home."""
    return shared.paths.ava_home() / "deploy-state.json"


def lock_path() -> Path:
    """Stable sibling lock; never replace/unlink the inode being locked."""
    return shared.paths.ava_home() / "deploy-state.lock"


def lifecycle_lock_path() -> Path:
    """Stable mutex for long local resource transitions and the hold probe."""
    return shared.paths.ava_home() / "deploy-state.lifecycle.lock"


def owner_lock_path() -> Path:
    """Short mutex for publishing ownership versus destructive recovery."""
    return shared.paths.ava_home() / "deploy-state.owner.lock"


def _holder_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.holder.json")


def _last_holder(path: Path) -> str:
    try:
        data = json.loads(_holder_path(path).read_text())
        return (
            f"pid={data['pid']} purpose={data['purpose']!r} "
            f"since={data['since']} state={data['state']}"
        )
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"


@contextlib.contextmanager
def _diagnostic_lock(path: Path, *, purpose: str, timeout_s: float) -> Generator[None]:
    """Hold an OS mutex with an atomic, best-effort postmortem holder record.

    The OS lock is authority; the sidecar is diagnostic only. Keep the last
    released holder so a timeout racing release still names the recent actor.
    """
    acquired = False
    try:
        with file_lock(path, timeout_s=timeout_s):
            acquired = True
            holder = {
                "pid": os.getpid(),
                "purpose": purpose,
                "since": dt.datetime.now(dt.UTC).isoformat(),
            }
            try:
                _write_atomic(_holder_path(path), {**holder, "state": "held"})
            except OSError:
                _log.warning("[ui-update-state] could not record lock holder", exc_info=True)
            try:
                yield
            finally:
                try:
                    _write_atomic(_holder_path(path), {**holder, "state": "released"})
                except OSError:
                    _log.warning(
                        "[ui-update-state] could not mark released lock holder", exc_info=True
                    )
    except LockTimeoutError as exc:
        if acquired:
            raise
        raise LockTimeoutError(
            f"{exc}; waiter purpose={purpose!r}; last holder: {_last_holder(path)}"
        ) from exc


@contextlib.contextmanager
def lifecycle_lock() -> Generator[None]:
    """Serialize short owner-publish and owner-recovery critical sections."""
    # The contextmanager generator enters through contextlib.__enter__; its
    # caller is the operation that actually owns this critical section.
    caller = sys._getframe(2)
    purpose = f"{caller.f_globals['__name__']}.{caller.f_code.co_name}"
    with _diagnostic_lock(owner_lock_path(), purpose=purpose, timeout_s=_LOCK_TIMEOUT_S):
        yield


@contextlib.contextmanager
def resource_lock(*, purpose: str, timeout_s: float = _RESOURCE_LOCK_TIMEOUT_S) -> Generator[None]:
    """Serialize long local start/stop/pause operations with bounded waiting."""
    with _diagnostic_lock(lifecycle_lock_path(), purpose=purpose, timeout_s=timeout_s):
        yield


def new_generation() -> str:
    """Mint an ownership token for a lock-winning writer's new generation."""
    return str(uuid.uuid4())


def _warn_invalid(message: str) -> None:
    """Rate-limit corrupt-state warnings on the gate's threaded request path."""
    now = time.monotonic()
    with _warning_lock:
        if now - _last_warning_at[0] < _WARNING_INTERVAL_S:
            return
        _last_warning_at[0] = now
    _log.warning("[ui-update-state] %s", message)


def _invalid(message: str) -> Never:
    raise ValueError(message)


def _timestamp(value: object, field: str) -> dt.datetime:
    if not isinstance(value, str):
        _invalid(f"{field} must be an RFC3339 string")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must carry a timezone")
    return parsed.astimezone(dt.UTC)


def _parse(data: object) -> UiUpdateSnapshot:
    if not isinstance(data, dict):
        _invalid("marker root must be an object")
    raw = cast("dict[str, object]", data)

    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(f"unknown schema_version {version!r}")
    if raw.get("state") != STATE_UPDATING:
        raise ValueError(f"unknown state {raw.get('state')!r}")
    generation = raw.get("generation")
    if not isinstance(generation, str) or not generation:
        raise ValueError("generation must be a non-empty string")
    kind = raw.get("kind")
    if kind not in _VALID_KINDS:
        raise ValueError(f"unknown update kind {kind!r}")
    phase = raw.get("phase")
    origin = raw.get("origin")
    if not isinstance(phase, str) or not phase:
        raise ValueError("phase must be a non-empty string")
    if not isinstance(origin, str):
        _invalid("origin must be a string")
    return UiUpdateSnapshot(
        status="updating",
        schema_version=SCHEMA_VERSION,
        generation=generation,
        kind=kind,
        started_at=_timestamp(raw.get("started_at"), "started_at"),
        updated_at=_timestamp(raw.get("updated_at"), "updated_at"),
        phase=phase,
        origin=origin,
    )


def _read_unlocked(path: Path) -> UiUpdateSnapshot:
    try:
        with path.open() as f:
            data = json.load(f)
    except FileNotFoundError:
        return UiUpdateSnapshot(status="inactive")
    except (OSError, ValueError) as exc:
        _warn_invalid(f"cannot read {path}: {exc!r}; projecting invalid/not-working")
        return UiUpdateSnapshot(status="invalid", error="marker unreadable")
    try:
        return _parse(data)
    except (TypeError, ValueError) as exc:
        _warn_invalid(f"invalid {path}: {exc}; projecting invalid/not-working")
        return UiUpdateSnapshot(status="invalid", error=str(exc))


def read(path: Path | str | None = None) -> UiUpdateSnapshot:
    """Read one complete marker snapshot; never raises on request paths."""
    return _read_unlocked(Path(path) if path is not None else state_path())


def _payload(
    *,
    generation: str,
    kind: UiUpdateKind,
    started_at: dt.datetime,
    updated_at: dt.datetime,
    phase: str,
    origin: str,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generation": generation,
        "state": STATE_UPDATING,
        "kind": kind,
        "started_at": started_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        "phase": phase,
        "origin": origin,
    }


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
    fd, raw_tmp = tempfile.mkstemp(dir=path.parent, prefix=".deploy-state-", suffix=".tmp")
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, separators=(",", ":"), sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # noqa: PTH105 — explicit atomic replace injection seam
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        _fsync_parent(path)
    except OSError:
        # os.replace is the visible commit point. Reporting failure after it
        # would tell the lock-winning child it has no maintenance owner even
        # though Gate can already observe one. Durability is degraded, but the
        # committed state/result must stay truthful.
        _log.warning("[ui-update-state] directory fsync failed after marker commit", exc_info=True)


def begin(
    *,
    kind: UiUpdateKind,
    origin: str,
    phase: str = "spawning",
    generation: str | None = None,
) -> UiUpdateSnapshot:
    """Create the DB-lock winner's active generation before any pause/stop."""
    if kind not in _VALID_KINDS:
        raise ValueError(f"invalid update kind: {kind!r}")
    path = state_path()
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        previous = _read_unlocked(path)
        if previous.status == "updating":
            raise UiUpdateAlreadyActive(
                f"UI update generation {previous.generation!r} is already active"
            )
        now = dt.datetime.now(dt.UTC)
        if generation is None:
            generation = new_generation()
        elif not generation:
            raise ValueError("generation must be non-empty")
        payload = _payload(
            generation=generation,
            kind=kind,
            started_at=now,
            updated_at=now,
            phase=phase,
            origin=origin,
        )
        _write_atomic(
            path,
            payload,
        )
        # Return the identity written while this lock was held. A lockless read
        # after release could observe recovery plus generation B and hand B's
        # identity to caller A, whose later cleanup would then delete B.
        return _parse(payload)


def set_phase(generation: str, phase: str) -> bool:
    """CAS-update diagnostics without changing a generation's start."""
    if not phase:
        raise ValueError("phase must be non-empty")
    path = state_path()
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        current = _read_unlocked(path)
        if current.status != "updating" or current.generation != generation:
            return False
        if current.kind is None or current.started_at is None or current.origin is None:
            return False
        now = dt.datetime.now(dt.UTC)
        _write_atomic(
            path,
            _payload(
                generation=generation,
                kind=current.kind,
                started_at=current.started_at,
                updated_at=now,
                phase=phase,
                origin=current.origin,
            ),
        )
        return True


def clear(generation: str) -> bool:
    """CAS-remove only the generation the caller owns."""
    path = state_path()
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        current = _read_unlocked(path)
        if current.status != "updating" or current.generation != generation:
            return False
        path.unlink(missing_ok=True)
        try:
            _fsync_parent(path)
        except OSError:
            _log.warning(
                "[ui-update-state] directory fsync failed after marker clear", exc_info=True
            )
        return True


def force_clear() -> bool:
    """Operator recovery: clear any marker after live-owner checks passed."""
    path = state_path()
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        existed = path.exists()
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        if existed:
            try:
                _fsync_parent(path)
            except OSError:
                _log.warning(
                    "[ui-update-state] directory fsync failed after forced marker clear",
                    exc_info=True,
                )
        return existed
