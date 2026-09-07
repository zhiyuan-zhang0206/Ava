"""Host-local capability journal for generation-scoped pause/resume.

The gateway's deploy lease proves who may pause a runner, but a compensating
resume must still work while the gateway database is down. The stop op copies
the exact ``(holder, acquired_at)`` capability into this atomic local journal
before pausing. Resume matches only that journal; a delayed generation A resume
can therefore never unpause generation B.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never, cast

from shared.maintenance_state import MaintenanceHold
from shared.platform import file_lock

_LOCK_TIMEOUT_S = 5.0
_log = logging.getLogger("shared.pause_owner")


def _invalid(message: str) -> Never:
    raise ValueError(message)


@dataclass(frozen=True)
class PauseOwnerSnapshot:
    status: Literal["inactive", "paused", "resumed", "legacy-resumed", "invalid"]
    holder: str | None = None
    acquired_at: dt.datetime | None = None
    maintenance: MaintenanceHold | None = None

    def matches(self, holder: str, acquired_at: dt.datetime) -> bool:
        return self.holder == holder and self.acquired_at == acquired_at


def state_path() -> Path:
    import shared.paths

    # Admission/status reads must not create a home before setup validation.
    # Writers create the parent through lock_path() and _write_atomic().
    return shared.paths.ava_home() / "run" / "deploy-pause-owner.json"


def lock_path() -> Path:
    import shared.paths

    return shared.paths.run_dir() / "deploy-pause-owner.lock"


def _read_unlocked(path: Path) -> PauseOwnerSnapshot:
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            _invalid("root must be an object")
        raw = cast("dict[str, object]", raw)
        state = raw["state"]
        if state == "legacy-resumed":
            return PauseOwnerSnapshot(status="legacy-resumed")
        holder = raw["holder"]
        acquired_raw = raw["acquired_at"]
        if state not in ("paused", "resumed"):
            _invalid("state must be paused or resumed")
        if not isinstance(holder, str) or not holder:
            _invalid("holder must be a non-empty string")
        if not isinstance(acquired_raw, str):
            _invalid("acquired_at must be RFC3339")
        acquired_at = dt.datetime.fromisoformat(acquired_raw.replace("Z", "+00:00"))
        if acquired_at.tzinfo is None:
            _invalid("acquired_at must carry a timezone")
        maintenance = MaintenanceHold.decode(raw["maintenance"]) if "maintenance" in raw else None
    except FileNotFoundError:
        return PauseOwnerSnapshot(status="inactive")
    except (KeyError, OSError, TypeError, ValueError) as exc:
        _log.warning("[pause-owner] invalid %s: %s", path, exc)
        return PauseOwnerSnapshot(status="invalid")
    return PauseOwnerSnapshot(
        status=state,
        holder=holder,
        acquired_at=acquired_at.astimezone(dt.UTC),
        maintenance=maintenance,
    )


def read_for_home(home: Path) -> PauseOwnerSnapshot:
    """Read the journal before configuration bootstrap, without creating the home."""
    return _read_unlocked(home / "run" / "deploy-pause-owner.json")


def read() -> PauseOwnerSnapshot:
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
    fd, raw_tmp = tempfile.mkstemp(dir=path.parent, prefix=".pause-owner-", suffix=".tmp")
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
        _log.warning("[pause-owner] directory fsync failed after commit", exc_info=True)


def mark_paused(holder: str, acquired_at: dt.datetime) -> PauseOwnerSnapshot:
    """Publish the DB-validated capability immediately before local pause."""
    if not holder or acquired_at.tzinfo is None:
        raise ValueError("holder and timezone-aware acquired_at are required")
    path = state_path()
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        current = _read_unlocked(path)
        if current.status == "paused" and current.matches(holder, acquired_at):
            return current
        _refuse_maintenance(current)
        _write_atomic(
            path,
            {
                "state": "paused",
                "holder": holder,
                "acquired_at": acquired_at.astimezone(dt.UTC).isoformat(),
            },
        )
        return _read_unlocked(path)


def mark_resumed(holder: str, acquired_at: dt.datetime) -> bool:
    """CAS-record completion of exactly the generation that was unpaused."""
    path = state_path()
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        current = _read_unlocked(path)
        if not current.matches(holder, acquired_at):
            return False
        if current.status == "resumed":
            return True
        if current.maintenance is not None:
            return False
        if current.status != "paused":
            return False
        _write_atomic(
            path,
            {
                "state": "resumed",
                "holder": holder,
                "acquired_at": acquired_at.astimezone(dt.UTC).isoformat(),
            },
        )
        return True


def mark_legacy_resumed() -> PauseOwnerSnapshot:
    """Record the one-rollout tokenless resume as an idempotent tombstone."""
    path = state_path()
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        current = _read_unlocked(path)
        if current.status not in ("inactive", "legacy-resumed"):
            raise RuntimeError("an exact or invalid pause owner replaced the legacy resume")
        _write_atomic(path, {"state": "legacy-resumed"})
        return _read_unlocked(path)


def finalize_natural_resume() -> bool:
    """Generation-scoped successful-finalize when a host returns to serving on
    its own, without a `cluster/resume` op.

    A rollout's Phase-B `ava start` (and the gateway-local finally's own
    unpause) restore posture directly, so the exact ``(holder, acquired_at)``
    the Phase-A stop journaled would otherwise stay ``paused`` forever even
    though the rollout finished — the 2026-08-26 residue (rollout rc=0 while
    deploy-pause-owner.json still read ``paused``). This records the journaled
    generation as ``resumed``, the same CAS record ``mark_resumed`` writes on
    the explicit resume path, under the same lock.

    Generation-scoped by construction, never a force-clear: only a ``paused``
    journal is transitioned, and only to its own generation — this never
    creates, mints or clears a record, and an absent / legacy /
    already-``resumed`` / ``invalid`` journal is left untouched (an invalid one
    may be cleared only by recovery's no-live-owner proof). A newer pause
    replaces the journal before a delayed finalize can reach it.

    Returns True when the journal was transitioned ``paused`` -> ``resumed``.
    """
    path = state_path()
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        current = _read_unlocked(path)
        if (
            current.maintenance is not None
            or current.status != "paused"
            or current.holder is None
            or current.acquired_at is None
        ):
            return False
        _write_atomic(
            path,
            {
                "state": "resumed",
                "holder": current.holder,
                "acquired_at": current.acquired_at.astimezone(dt.UTC).isoformat(),
            },
        )
        return True


def clear(holder: str, acquired_at: dt.datetime) -> bool:
    """CAS-clear one recovered/completed generation; never a replacement."""
    path = state_path()
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        current = _read_unlocked(path)
        if current.maintenance is not None:
            return False
        if not current.matches(holder, acquired_at):
            return False
        path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            _fsync_parent(path)
        return True


def force_clear() -> bool:
    """Explicit recovery of malformed state after its no-live-owner proof."""
    path = state_path()
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        _refuse_maintenance(_read_unlocked(path))
        existed = path.exists()
        path.unlink(missing_ok=True)
        if existed:
            with contextlib.suppress(OSError):
                _fsync_parent(path)
        return existed


def _refuse_maintenance(current: PauseOwnerSnapshot) -> None:
    if current.status == "paused" and current.maintenance is not None:
        raise RuntimeError("maintenance requires its exact operation's explicit resume")


def begin_maintenance(holder: str, acquired_at: dt.datetime) -> PauseOwnerSnapshot:
    """Close admission durably; a new deploy cannot overwrite this capability."""
    if not holder or acquired_at.tzinfo is None:
        raise ValueError("holder and timezone-aware acquired_at are required")
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        current = _read_unlocked(state_path())
        if current.matches(holder, acquired_at) and current.maintenance is not None:
            return current
        if current.status == "invalid" or (
            current.status == "paused" and not current.matches(holder, acquired_at)
        ):
            raise RuntimeError("another or unreadable pause owner must be resolved first")
        return _write_maintenance(holder, acquired_at, MaintenanceHold())


def _write_maintenance(
    holder: str, acquired_at: dt.datetime, hold: MaintenanceHold, *, resumed: bool = False
) -> PauseOwnerSnapshot:
    _write_atomic(
        state_path(),
        {
            "state": "resumed" if resumed else "paused",
            "holder": holder,
            "acquired_at": acquired_at.astimezone(dt.UTC).isoformat(),
            "maintenance": hold.encode(),
        },
    )
    return _read_unlocked(state_path())


def change_maintenance(
    holder: str,
    acquired_at: dt.datetime,
    expected: MaintenanceHold,
    replacement: MaintenanceHold,
    *,
    resumed: bool = False,
) -> PauseOwnerSnapshot:
    """CAS a caller-validated maintenance transition without losing receipts."""
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        current = _read_unlocked(state_path())
        if (
            current.status != "paused"
            or not current.matches(holder, acquired_at)
            or current.maintenance != expected
        ):
            raise RuntimeError("maintenance operation or progress changed; reread before retry")
        return _write_maintenance(holder, acquired_at, replacement, resumed=resumed)
