"""What the daily tick has done, in one owner-only file the health probe reads.

`$AVA_HOME/backups/walg/state.json` (0600, replaced atomically) is the only thing
the tick and the probe share. Every alert about the tick is a condition on it, so
its sections are facts, not messages:

- `tick`: when the latest tick started, and why it did nothing if it skipped. Written
  first, at the start of every tick: this is what "the tick is still running" is read from.
- `run`: the outcome of the latest tick that actually ran its steps (`ok` or `failed`,
  and the step that failed). A skipped tick leaves it alone, so a failure stays
  visible until a run succeeds.
- `backup`, `verify`, `retention`: the last result of each step.
- `drill`: the latest recovery drill (`drill.py`) and when one last succeeded. Written by
  whoever runs the drill: the tick, or `ava backup walg drill`, both under the lock.

Only the tick writes, and only while holding the per-home run lock; readers never
lock. A missing file is an empty state; an unreadable or malformed one is an error
(`StateError`), never silently an empty state.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from base.deploy.release.verified_file import RegularFileReadError, regular_bytes
from base.host.private_storage import write_private_bytes
from base.paths import ava_home

_VERSION = 1
_MAX_BYTES = 256 * 1024

STEP_PREFLIGHT = "preflight"
STEP_BACKUP = "backup"
STEP_VERIFY = "verify"
STEP_RETENTION = "retention"
STEPS = (STEP_PREFLIGHT, STEP_BACKUP, STEP_VERIFY, STEP_RETENTION)
"""The tick's steps in order; `RunRecord.step` names the one that failed."""

RunStatus = Literal["ok", "failed"]
_RUN_STATUSES = ("ok", "failed")


class StateError(RuntimeError):
    """The state file exists but cannot be read as this code writes it."""


def walg_dir() -> Path:
    return ava_home() / "backups" / "walg"


def state_path() -> Path:
    return walg_dir() / "state.json"


def lock_path() -> Path:
    return walg_dir() / "run.lock"


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("state timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _parse(value: object) -> datetime:
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        raise ValueError("state timestamp is naive")
    return stamp.astimezone(UTC)


@dataclass(frozen=True)
class TickRecord:
    started_at: datetime
    skipped: str | None = None


@dataclass(frozen=True)
class RunRecord:
    started_at: datetime
    finished_at: datetime
    status: RunStatus
    step: str | None  # the step that failed; None when the run succeeded
    detail: str


@dataclass(frozen=True)
class BackupRecord:
    name: str
    kind: Literal["full", "delta"]
    finished_at: datetime
    uncompressed_bytes: int
    compressed_bytes: int


@dataclass(frozen=True)
class VerifyRecord:
    at: datetime
    integrity: str
    timeline: str


@dataclass(frozen=True)
class RetentionRecord:
    at: datetime
    marked: int
    deleted: int


@dataclass(frozen=True)
class DrillRecord:
    """One recovery drill. A failed drill keeps `last_ok_at` of the last success, so
    "no drill has succeeded lately" stays readable across failures."""

    finished_at: datetime
    ok: bool
    backup: str
    target_lsn: str | None  # None: no target (nothing newer than the backup was archived)
    seconds: float
    detail: str
    last_ok_at: datetime | None


@dataclass(frozen=True)
class State:
    tick: TickRecord | None = None
    run: RunRecord | None = None
    backup: BackupRecord | None = None
    verify: VerifyRecord | None = None
    retention: RetentionRecord | None = None
    drill: DrillRecord | None = None


def _timestamps_to_json(record: object) -> dict[str, Any]:
    return {
        key: _stamp(value) if isinstance(value, datetime) else value
        for key, value in asdict(cast(Any, record)).items()
    }


def to_json(state: State) -> bytes:
    sections: dict[str, Any] = {"version": _VERSION}
    for name in ("tick", "run", "backup", "verify", "retention", "drill"):
        record = getattr(state, name)
        sections[name] = None if record is None else _timestamps_to_json(record)
    return (json.dumps(sections, indent=2, sort_keys=True) + "\n").encode()


def _tick(r: dict[str, Any]) -> TickRecord:
    skipped = r["skipped"]
    return TickRecord(
        started_at=_parse(r["started_at"]), skipped=None if skipped is None else str(skipped)
    )


def _run(r: dict[str, Any]) -> RunRecord:
    status, step = r["status"], r["step"]
    if status not in _RUN_STATUSES:
        raise StateError(f"unknown run status {status!r}")
    if step is not None and step not in STEPS:
        raise StateError(f"unknown run step {step!r}")
    return RunRecord(
        started_at=_parse(r["started_at"]),
        finished_at=_parse(r["finished_at"]),
        status=status,
        step=None if step is None else str(step),
        detail=str(r["detail"]),
    )


def _backup(r: dict[str, Any]) -> BackupRecord:
    kind = r["kind"]
    if kind not in ("full", "delta"):
        raise StateError(f"unknown backup kind {kind!r}")
    return BackupRecord(
        name=str(r["name"]),
        kind=kind,
        finished_at=_parse(r["finished_at"]),
        uncompressed_bytes=int(r["uncompressed_bytes"]),
        compressed_bytes=int(r["compressed_bytes"]),
    )


def _verify(r: dict[str, Any]) -> VerifyRecord:
    return VerifyRecord(
        at=_parse(r["at"]), integrity=str(r["integrity"]), timeline=str(r["timeline"])
    )


def _retention(r: dict[str, Any]) -> RetentionRecord:
    return RetentionRecord(at=_parse(r["at"]), marked=int(r["marked"]), deleted=int(r["deleted"]))


def _drill(r: dict[str, Any]) -> DrillRecord:
    target_lsn, last_ok_at = r["target_lsn"], r["last_ok_at"]
    ok = r["ok"]
    if not isinstance(ok, bool):
        raise StateError(f"drill ok is {ok!r}, not a boolean")
    return DrillRecord(
        finished_at=_parse(r["finished_at"]),
        ok=ok,
        backup=str(r["backup"]),
        target_lsn=None if target_lsn is None else str(target_lsn),
        seconds=float(r["seconds"]),
        detail=str(r["detail"]),
        last_ok_at=None if last_ok_at is None else _parse(last_ok_at),
    )


def _section[R](raw: dict[str, Any], name: str, build: Callable[[dict[str, Any]], R]) -> R | None:
    body = raw[name]
    return None if body is None else build(cast(dict[str, Any], body))


def from_json(data: bytes) -> State:
    """Parse a state file; every key is required, a missing one is an error.

    Raises:
        StateError: not JSON, a different version, or a section of the wrong shape.
    """
    try:
        raw = cast(dict[str, Any], json.loads(data))
        version = raw["version"]
    except (ValueError, KeyError, TypeError):
        raise StateError("the WAL-G state file is malformed") from None
    if version != _VERSION:
        raise StateError(f"state file version {version!r} is not {_VERSION}")
    try:
        return State(
            tick=_section(raw, "tick", _tick),
            run=_section(raw, "run", _run),
            backup=_section(raw, "backup", _backup),
            verify=_section(raw, "verify", _verify),
            retention=_section(raw, "retention", _retention),
            drill=_section(raw, "drill", _drill),
        )
    except StateError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError):
        raise StateError("the WAL-G state file is malformed") from None


def read_state() -> State:
    """The recorded state; an empty one before the first tick.

    Raises:
        StateError: the file exists but is unreadable or malformed.
    """
    path = state_path()
    try:
        data = regular_bytes(path, max_bytes=_MAX_BYTES)
    except FileNotFoundError:
        return State()
    except (OSError, RegularFileReadError):
        raise StateError(f"the WAL-G state file {path} is not readable") from None
    return from_json(data)


def write_state(state: State) -> None:
    """Replace the state file atomically (owner-only). Call only under the run lock."""
    write_private_bytes(state_path(), to_json(state))


def update_state(**sections: Any) -> State:
    """Read, replace the named sections, write; returns the new state."""
    state = replace(read_state(), **sections)
    write_state(state)
    return state
