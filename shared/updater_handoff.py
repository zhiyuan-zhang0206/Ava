"""Retained host-local updater handoff evidence: readers and exact-generation clear.

The retired in-place updater published ``$AVA_HOME/run/updater-handoff.json``
(a ``pending`` generation, then ``running`` with the owner's PID + process
birth time) and a versioned bootstrap/normal recovery envelope beside it. No
current code writes either file. A host upgraded from that updater may still
carry them, so cluster resume and recovery read them and refuse while they
could name a live owner or unfinished compensation.

It is not the deployment UI marker and Gate never reads it. Pending expiry only
opens a recovery attempt; it never proves a running child dead. Recovery may
clear a running generation only when PID + birth time prove that owner gone,
and only when the retained recovery envelope is terminal.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never, cast

import psutil

import shared.paths
from shared import atomic_io
from shared.native_process.ownership import create_time_matches, stable_create_time
from shared.platform import file_lock
from shared.updater_recovery import BootstrapRecoveryJournal

_LOCK_TIMEOUT_S = 5.0
_BOOTSTRAP_RECOVERY_VERSION = 1
_MAX_BOOTSTRAP_RECOVERY_BYTES = 256 * 1024
# A generation token read back from the marker: the session-name character
# class, so a tampered marker can never steer the clear-time GC out of
# `run/updater-spawn/`.
_GENERATION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_log = logging.getLogger("shared.updater_handoff")


class BootstrapRecoveryInvalidError(RuntimeError):
    """Retained compensating evidence is present but cannot be authenticated."""


def _parse_bootstrap_journal(value: object) -> BootstrapRecoveryJournal:
    try:
        return BootstrapRecoveryJournal.model_validate_json(
            json.dumps(value, separators=(",", ":"))
        )
    except (TypeError, ValueError) as exc:
        raise BootstrapRecoveryInvalidError("bootstrap recovery journal is malformed") from exc


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


def state_path() -> Path:
    return shared.paths.run_dir() / "updater-handoff.json"


def bootstrap_state_path() -> Path:
    """Versioned compensating evidence, separate from ordinary spawn ownership."""
    return shared.paths.run_dir() / "updater-bootstrap-recovery.json"


def lock_path() -> Path:
    return shared.paths.run_dir() / "updater-handoff.lock"


def spawn_attempts_dir(generation: str) -> Path:
    """This unit's per-generation spawn-attempt evidence directory (I6 GC scope).

    The retired updater's gated spawns left their receipts and gates under
    ``run/updater-spawn/<generation>``. Nothing writes there now; clear still
    removes a retained generation's directory. The name check keeps that GC
    inside the directory whatever the marker says.
    """
    if not _GENERATION_PATTERN.fullmatch(generation):
        raise ValueError("generation is not a valid spawn-attempt directory name")
    return shared.paths.ava_home() / "run" / "updater-spawn" / generation


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
    atomic_io.fsync_parent(path)


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
        journal = _parse_bootstrap_journal(cast("dict[str, object]", envelope["journal"]))
        envelope["journal"] = journal.model_dump(mode="json")
        return envelope
    except FileNotFoundError:
        return None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BootstrapRecoveryInvalidError("bootstrap recovery envelope is malformed") from exc


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
        actual = stable_create_time(psutil.Process(snapshot.owner_pid))
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, psutil.Error, OSError):
        return True
    return create_time_matches(actual, snapshot.owner_create_time)


def _bootstrap_clearable_unlocked(generation: str) -> bool:
    try:
        bootstrap = _read_bootstrap_unlocked()
    except BootstrapRecoveryInvalidError:
        return False
    if bootstrap is None:
        return True
    journal = _parse_bootstrap_journal(bootstrap["journal"])
    if bootstrap["generation"] != generation or journal.stage not in {
        "candidate_ready",
        "recovered",
    }:
        return False
    if journal.normal_release is not None and (
        not journal.normal_release_planned or journal.stage != "candidate_ready"
    ):
        return False
    if journal.normal_release is None:
        return journal.stage == "recovered" or not journal.normal_release_planned
    return journal.normal_release.stage == "committed"


def allows_generic_recovery(snapshot: UpdaterHandoffSnapshot) -> bool:
    """Whether generic unpause may safely discard this exact dead handoff."""
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        if _read_unlocked(state_path()) != snapshot:
            return False
        if snapshot.generation is None:
            return not bootstrap_state_path().exists()
        return _bootstrap_clearable_unlocked(snapshot.generation)


def _gc_spawn_attempts(generation: str) -> None:
    """I6: delete this generation's spawn-attempt evidence -- the ONLY path.

    Clear-time is the single deletion point for receipts and gates (design
    §4.5/§5): the directory is removed BEFORE the state-file unlinks, so every
    crash point replays to convergence -- while a state file still names the
    generation, the next clear re-enters, re-GCs (a no-op) and finishes. A
    failed removal is logged and kept: retaining evidence is the conservative
    side, and clear's verdict is about the handoff state, not the directory.
    """
    try:
        shutil.rmtree(spawn_attempts_dir(generation))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        _log.warning("[updater-handoff] spawn-attempt GC left evidence in place: %s", exc)


def clear(generation: str) -> bool:
    """CAS-clear only the handoff generation the caller owns."""
    path = state_path()
    with file_lock(lock_path(), timeout_s=_LOCK_TIMEOUT_S):
        current = _read_unlocked(path)
        if current.generation != generation:
            return False
        if not _bootstrap_clearable_unlocked(generation):
            return False
        _gc_spawn_attempts(generation)
        if bootstrap_state_path().exists():
            bootstrap_state_path().unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            _fsync_parent(path)
        return True
