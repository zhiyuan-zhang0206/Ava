"""Record liveness + enumeration for pty sessions (no process to dial —
the records ARE the session listing; a dead record is swept as it is
discovered).

Split out of ``cli.py`` (2026-09-09, task #2670): the host-aware liveness
rule and the lazy sweep grew the op CLI past its file-size ceiling. Every
caller — the CLI, the session backend, the page-server daemon's in-process
scan — reads the same liveness definition from here.
"""

from __future__ import annotations

import contextlib
import os
import signal
from pathlib import Path

import psutil

from shared.log import logger
from shared.pty_sessions._paths import (
    host_identity,
    host_starttime,
    pty_dir,
    record_path,
    socket_path,
)
from shared.session_record import SessionRecord, pid_starttime_ticks

# Record liveness + enumeration (no process to dial — the records ARE the
# session listing; a dead record is swept as it is discovered).
# ---------------------------------------------------------------------------

# Legacy epoch identity tolerance (mirrors posixproc).
_CREATE_TIME_TOLERANCE_S = 2.0


def _host_is_alive(path: Path) -> bool:
    """Whether a pty record's host process is alive with matching identity.

    Records written before host identity existed (legacy) read alive — the
    host state is unknown, not dead. A gone, zombie, or recycled host pid
    reads dead: no host means no socket, screen model, or kill protocol, so
    the session cannot answer any op even while its shell survives the
    host's unclean death (task #2670).
    """
    identity = host_identity(path)
    if identity is None:
        return True
    host_pid, host_create = identity
    try:
        proc = psutil.Process(host_pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        recorded_starttime = host_starttime(path)
        if recorded_starttime is not None:
            return pid_starttime_ticks(host_pid) == recorded_starttime
        return abs(proc.create_time() - host_create) <= _CREATE_TIME_TOLERANCE_S
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        # An unreadable host is unknown, not dead: the teardown below only
        # fires on provable death, so a permissions blip must never sweep a
        # healthy session.
        return True


def _record_alive(rec: SessionRecord, path: Path) -> bool:
    """Whether a pty record names a live session: a live, identity-matching
    shell AND (when the record carries host identity) a live, matching host.

    The shell alone is not sufficient — a host that died uncleanly (SIGKILL
    by the orphan reaper, OOM, a crash) can leave its shell running while
    the socket, screen, and kill protocol die with the host. Such a record
    must read dead so listings filter it and the lazy sweep replaces it,
    instead of reporting a session that answers no op (2026-09-09
    page-ghost incident, task #2670). Legacy records without host identity
    keep the shell-only rule — the host state is unknown, not dead.
    """
    try:
        proc = psutil.Process(rec.pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        if rec.starttime is not None:
            if rec.identifies(rec.pid) is not True:
                return False
        elif abs(proc.create_time() - rec.create_time) > _CREATE_TIME_TOLERANCE_S:
            return False
    except psutil.Error:
        return False
    return _host_is_alive(path)


def has_session(name: str) -> bool:
    """True if the named pty session's record points at a live, matching
    shell process — the same liveness rule posixproc applies to its records."""
    path = record_path(name)
    rec = SessionRecord.read(path)
    return rec is not None and _record_alive(rec, path)


def session_started_at(name: str) -> float | None:
    """Epoch seconds the named pty session was launched, or None when it is
    not alive. Same record + pid liveness rule as `has_session`."""
    path = record_path(name)
    rec = SessionRecord.read(path)
    if rec is None or not _record_alive(rec, path):
        return None
    return rec.started_at


def session_generation(name: str) -> str | None:
    """The live PTY session's persisted flip generation, if one exists."""
    path = record_path(name)
    rec = SessionRecord.read(path)
    if rec is None or not _record_alive(rec, path):
        return None
    return rec.generation


# Last warning reason per retained record: a tick-scanning poller (the
# page-server daemon scans every ~2s pass) would otherwise flood the log with
# an identical warning every pass. Entries exist only while a record keeps
# failing liveness; a swept record's entry is dropped so a re-created record
# warns again.
_retained_warning_reasons: dict[str, str] = {}


def _kill_recorded_shell(rec: SessionRecord) -> None:
    """SIGKILL a record's shell after its host is provably gone.

    The host owns the orderly teardown; with the host gone the shell is an
    orphan that must not linger (it can hold the page server process). The
    kill is identity-gated exactly like ``_kill_by_record``'s — a recycled
    pid is never signalled.
    """
    try:
        proc = psutil.Process(rec.pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return
        if rec.starttime is not None:
            if rec.identifies(rec.pid) is not True:
                return
        elif abs(proc.create_time() - rec.create_time) > _CREATE_TIME_TOLERANCE_S:
            return
    except psutil.Error:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(rec.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, OSError):
        os.kill(rec.pid, signal.SIGKILL)


def _sweep_dead(name: str) -> None:
    """Drop a provably dead session's record + socket (its host is gone too)."""
    path = record_path(name)
    rec = SessionRecord.read(path)
    if rec is not None:
        reapable, why = _record_reapable(path, rec)
        if not reapable:
            if _retained_warning_reasons.get(name) != why:
                logger.warning(
                    "pty retaining live session record {name}: {why}",
                    name=name,
                    why=why,
                )
                _retained_warning_reasons[name] = why
            return
    _retained_warning_reasons.pop(name, None)
    with contextlib.suppress(OSError):
        record_path(name).unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        socket_path(name).unlink(missing_ok=True)


def _record_reapable(path: Path, rec: SessionRecord) -> tuple[bool, str]:
    """Whether a pty record that failed liveness can safely be swept."""
    if _record_alive(rec, path):
        return False, "shell is still live"
    try:
        proc = psutil.Process(rec.pid)
        if not proc.is_running():
            return True, "shell pid is no longer running"
        if proc.status() == psutil.STATUS_ZOMBIE:
            return True, "shell exited and awaits parent reap"
    except psutil.NoSuchProcess:
        return True, "shell pid is gone"
    except (psutil.AccessDenied, OSError):
        return False, "shell pid could not be inspected"
    if rec.identifies(rec.pid) is False:
        return True, "shell pid was reused by another process"
    # The record names a host that is provably gone while its shell survived:
    # the session is terminal for every op (no socket, no screen, no kill
    # protocol). Tear the identity-matched orphan shell down and sweep.
    if not _host_is_alive(path):
        _kill_recorded_shell(rec)
        return True, "session host is gone"
    return False, "live shell pid did not satisfy the legacy identity check"


def live_sessions(prefix: str = "") -> dict[str, SessionRecord]:
    """Every live session's record, filtered by name prefix.

    The record scan is the session listing; a record whose shell is gone is
    a crashed host's leftover and is swept as it is discovered (the same
    lazy sweep posixproc.list_sessions performs on its dir).
    """
    out: dict[str, SessionRecord] = {}
    for rec_file in sorted(pty_dir().glob("*.json")):
        name = rec_file.stem
        if not name.startswith(prefix):
            continue
        rec = SessionRecord.read(rec_file)
        if rec is None or not _record_alive(rec, rec_file):
            _sweep_dead(name)
            continue
        out[name] = rec
    return out
