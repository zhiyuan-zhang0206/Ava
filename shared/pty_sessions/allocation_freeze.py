"""Host-wide, generation-owned admission freeze for new PTY sessions.

Every co-located Ava cluster consumes the same host PTY pool, so the marker and
its lock live beside the host-level cluster registry rather than under one
``$AVA_HOME``. Existing sessions are unaffected. The one protected transition
is absent session -> new detached PTY host.

The marker is an operator capability: only its exact random generation may
resume allocation. Readers classify malformed state as ``invalid`` so the
allocation path fails closed instead of guessing that a corrupt marker means
unfrozen.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import tempfile
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never, cast

from shared.platform import file_lock

SCHEMA_VERSION = 1
_LOCK_TIMEOUT_S = 60.0
_STATE_FILENAME = "pty-allocation-freeze.json"
_LOCK_FILENAME = "pty-allocation.lock"

_log = logging.getLogger("shared.pty_sessions.allocation_freeze")


@dataclass(frozen=True)
class PtyAllocationFreeze:
    """Complete projection of the host allocation marker."""

    status: Literal["inactive", "frozen", "invalid"]
    generation: str | None = None
    holder: str | None = None
    reason: str | None = None
    created_at: dt.datetime | None = None
    error: str | None = None


class PtyAllocationAlreadyFrozenError(RuntimeError):
    """A valid generation already owns the allocation freeze."""


class InvalidPtyAllocationFreezeError(RuntimeError):
    """The marker is corrupt, so allocation remains frozen fail-closed."""


def _invalid(message: str) -> Never:
    raise ValueError(message)


def _host_state_dir() -> Path:
    """Directory shared by every cluster home on this host.

    ``AVA_CLUSTER_REGISTRY`` is already Ava's declared host-level path. Using
    its parent keeps test and custom installations isolated without inventing a
    second host-root setting.
    """
    from shared.config import settings

    return Path(settings.general.cluster_registry).expanduser().parent


def state_path() -> Path:
    return _host_state_dir() / _STATE_FILENAME


def lock_path() -> Path:
    return _host_state_dir() / _LOCK_FILENAME


def _timestamp(value: object) -> dt.datetime:
    if not isinstance(value, str):
        _invalid("created_at must be an RFC3339 string")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        _invalid("created_at must carry a timezone")
    return parsed.astimezone(dt.UTC)


def _parse(raw_value: object) -> PtyAllocationFreeze:
    if not isinstance(raw_value, dict):
        _invalid("marker root must be an object")
    raw = cast("dict[str, object]", raw_value)
    if raw.get("schema_version") != SCHEMA_VERSION:
        _invalid(f"unknown schema_version {raw.get('schema_version')!r}")
    if raw.get("state") != "frozen":
        _invalid(f"unknown state {raw.get('state')!r}")
    generation = raw.get("generation")
    holder = raw.get("holder")
    reason = raw.get("reason")
    if not isinstance(generation, str) or not generation:
        _invalid("generation must be a non-empty string")
    if not isinstance(holder, str) or not holder:
        _invalid("holder must be a non-empty string")
    if not isinstance(reason, str) or not reason:
        _invalid("reason must be a non-empty string")
    return PtyAllocationFreeze(
        status="frozen",
        generation=generation,
        holder=holder,
        reason=reason,
        created_at=_timestamp(raw.get("created_at")),
    )


def _read_unlocked(path: Path) -> PtyAllocationFreeze:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return PtyAllocationFreeze(status="inactive")
    except (OSError, ValueError) as exc:
        _log.warning("[pty-allocation] cannot read %s: %s", path, exc)
        return PtyAllocationFreeze(status="invalid", error=f"marker unreadable: {exc}")
    try:
        return _parse(raw)
    except (TypeError, ValueError) as exc:
        _log.warning("[pty-allocation] invalid %s: %s", path, exc)
        return PtyAllocationFreeze(status="invalid", error=str(exc))


def read() -> PtyAllocationFreeze:
    """Read one atomic marker snapshot without taking the allocation lock."""
    return _read_unlocked(state_path())


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
    fd, raw_tmp = tempfile.mkstemp(dir=path.parent, prefix=".pty-allocation-freeze-", suffix=".tmp")
    if os.name != "nt":
        os.fchmod(fd, 0o600)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)  # noqa: PTH105 — the replace is the atomic commit point
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        _fsync_parent(path)
    except OSError:
        # The replace already committed visible state. Do not report that the
        # caller failed to freeze when every new-session path now sees frozen.
        _log.warning("[pty-allocation] directory fsync failed after marker commit", exc_info=True)


@contextlib.contextmanager
def locked_freeze_state() -> Generator[PtyAllocationFreeze]:
    """Hold the host allocation mutex and yield its current marker state.

    ``_op_new`` keeps this lock until its host answers ready. Therefore, after
    ``freeze`` returns, every earlier allocation is fully visible and every
    later absent-to-live transition observes the marker and is refused.
    """
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        yield _read_unlocked(state_path())


def freeze(*, holder: str, reason: str) -> PtyAllocationFreeze:
    """Create a new operator-owned allocation freeze generation."""
    if not holder:
        raise ValueError("holder must be non-empty")
    if not reason:
        raise ValueError("reason must be non-empty")
    path = state_path()
    with locked_freeze_state() as current:
        if current.status == "invalid":
            raise InvalidPtyAllocationFreezeError(
                f"cannot replace invalid marker {path}: {current.error}"
            )
        if current.status == "frozen":
            raise PtyAllocationAlreadyFrozenError(
                f"generation {current.generation!r} held by {current.holder!r} is already active"
            )
        now = dt.datetime.now(dt.UTC)
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "state": "frozen",
            "generation": str(uuid.uuid4()),
            "holder": holder,
            "reason": reason,
            "created_at": now.isoformat(),
        }
        _write_atomic(path, payload)
        return _parse(payload)


def resume(generation: str) -> bool:
    """CAS-clear exactly ``generation``; never clear a replacement owner."""
    if not generation:
        raise ValueError("generation must be non-empty")
    path = state_path()
    with locked_freeze_state() as current:
        if current.status == "invalid":
            raise InvalidPtyAllocationFreezeError(
                f"cannot resume invalid marker {path}: {current.error}"
            )
        if current.status != "frozen" or current.generation != generation:
            return False
        path.unlink()
        with contextlib.suppress(OSError):
            _fsync_parent(path)
        return True
