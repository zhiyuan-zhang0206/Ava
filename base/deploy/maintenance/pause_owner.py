"""Host-local capability journal for generation-scoped pause/resume.

A maintenance hold (`begin_maintenance`) records its exact ``(holder,
acquired_at)`` capability in this atomic local journal, so a compensating
resume still works while the gateway database is down. Every transition
matches only that journal; a delayed generation A resume can therefore never
unpause generation B. A record without a maintenance hold, like an unreadable
one, reads as invalid and only an operator's `rm` of the journal removes it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never, cast

from base.deploy.maintenance.state import MaintenanceHold
from base.host.atomic_io import fsync_parent, write_text_atomic
from base.native_process.os_platform import file_lock

_LOCK_TIMEOUT_S = 5.0
_log = logging.getLogger("base.deploy.maintenance.pause_owner")


def _invalid(message: str) -> Never:
    raise ValueError(message)


@dataclass(frozen=True)
class PauseOwnerSnapshot:
    status: Literal["inactive", "paused", "resumed", "invalid"]
    holder: str | None = None
    acquired_at: dt.datetime | None = None
    maintenance: MaintenanceHold | None = None

    def matches(self, holder: str, acquired_at: dt.datetime) -> bool:
        return self.holder == holder and self.acquired_at == acquired_at


@dataclass(frozen=True)
class MaintenanceAdmission:
    snapshot: PauseOwnerSnapshot
    created_here: bool


def state_path() -> Path:
    import base.paths

    # Admission/status reads must not create a home before setup validation.
    # Writers create the parent through lock_path() and _write_atomic().
    return base.paths.ava_home() / "run" / "deploy-pause-owner.json"


def lock_path() -> Path:
    import base.paths

    return base.paths.run_dir() / "deploy-pause-owner.lock"


def _read_unlocked(path: Path) -> PauseOwnerSnapshot:
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            _invalid("root must be an object")
        raw = cast("dict[str, object]", raw)
        state = raw["state"]
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
        maintenance = MaintenanceHold.decode(raw["maintenance"])
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
    fsync_parent(path)


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        path,
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        mode=0o600,
        prefix=".pause-owner-",
    )
    try:
        _fsync_parent(path)
    except OSError:
        _log.warning("[pause-owner] directory fsync failed after commit", exc_info=True)


def begin_maintenance(holder: str, acquired_at: dt.datetime) -> MaintenanceAdmission:
    """Close admission durably; a new deploy cannot overwrite this capability.

    Creation ownership is returned under the journal lock: compensation must
    never infer it from a separately read snapshot.
    """
    if not holder or acquired_at.tzinfo is None:
        raise ValueError("holder and timezone-aware acquired_at are required")
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        current = _read_unlocked(state_path())
        if current.matches(holder, acquired_at) and current.status == "resumed":
            raise RuntimeError("maintenance operation already resumed; use a new generation")
        if current.matches(holder, acquired_at) and current.maintenance is not None:
            return MaintenanceAdmission(current, created_here=False)
        if current.status == "invalid" or (
            current.status == "paused" and not current.matches(holder, acquired_at)
        ):
            raise RuntimeError("another or unreadable pause owner must be resolved first")
        return MaintenanceAdmission(
            _write_maintenance(holder, acquired_at, MaintenanceHold()),
            created_here=True,
        )


def _write_maintenance(
    holder: str,
    acquired_at: dt.datetime,
    hold: MaintenanceHold,
    *,
    resumed: bool = False,
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
