"""Durable status journal for hosted lifecycle operations (pause / stop / restart).

A lifecycle command can outlive its caller: an outer budget (a subprocess
timeout on `ava restart`, a dropped SSH session) kills the CLI process
mid-operation, and stdout — the only place phase progress and the final
diagnosis were printed — is lost with it. This journal records the operation's
identity, deadline, phase timeline and final diagnosis at
``$AVA_HOME/run/lifecycle-op.json``, written atomically after every phase, so
the outcome stays readable after the process is gone (issue #2123).

Writers never fail the operation they journal: a journal write failure is
reported on stderr and the stop/restart proceeds.

Durability boundary: writes are atomic (temp file + rename) but never
fsynced. The journal exists to outlive its WRITER PROCESS — a kill or an
outer timeout leaves the page cache intact, so the file stays readable — not
to survive a machine crash: a power loss may lose the last write, which this
diagnostic journal accepts instead of paying an fsync on every phase.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

import psutil

from shared.log import logger
from shared.paths import run_dir


@dataclass
class LifecyclePhase:
    name: str
    started_at: float
    finished_at: float | None = None
    ok: bool | None = None


@dataclass
class ResumedFrom:
    """Identity of the finished run whose journal this operation took over.

    The previous writer's process is gone (the cut-off this journal exists
    for): `begin` refreshed the journal's pid and carried the dead run's
    phase timeline forward, recording the superseded identity here.
    """

    pid: int
    operation: str
    started_at: float


@dataclass
class LifecycleOp:
    operation: str
    pid: int
    started_at: float
    deadline: float | None
    phases: list[LifecyclePhase] = field(default_factory=list[LifecyclePhase])
    complete: bool = False
    result: dict[str, Any] | None = None
    resumed_from: ResumedFrom | None = None


def _path() -> Path:
    return run_dir() / "lifecycle-op.json"


def _write(op: LifecycleOp) -> None:
    """Atomic best-effort write; a failure is loud but never fails the op."""
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".lifecycle-op.", suffix=".tmp")
        try:
            with os.fdopen(_fd, "w") as f:
                json.dump(asdict(op), f)
            Path(tmp).replace(path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp).unlink(missing_ok=True)
            raise
    except (OSError, ValueError) as exc:
        logger.warning("lifecycle status journal write failed: {}", exc)


def _writer_alive(op: LifecycleOp) -> bool:
    """Whether `op`'s writer process can still be recording into the journal.

    A zombie counts as dead: it has finished executing and can never write
    another phase. An unreadable pid (AccessDenied) counts as alive — the
    conservative reading, so a possibly-live outer operation's journal is
    kept instead of being clobbered.
    """
    try:
        proc = psutil.Process(op.pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError):
        return True


def begin(operation: str, *, deadline: float | None = None) -> bool:
    """Start journaling `operation` (pause / stop / restart).

    Returns True when this call OWNS the journal: it started a fresh one,
    replaced a completed one, or took over an unfinished one whose writer is
    gone. A takeover refreshes the journal's pid to this process, keeps the
    dead run's phase timeline, and names the superseded writer in
    `resumed_from` — the journal must never keep reporting a dead pid as its
    current operation (task #2898). An outer operation's still-running
    journal (a restart wrapping a stop leg) is kept — the inner caller then
    records phases into it but never begins or finishes it.

    Boundary: ownership is judged by pid liveness alone. A stale journal
    whose pid a later process recycled reads as still-running and is not
    taken over — the conservative direction.
    """
    op = read()
    if op is not None and not op.complete:
        if _writer_alive(op):
            return False
        _write(
            LifecycleOp(
                operation=operation,
                pid=os.getpid(),
                started_at=time.monotonic(),
                deadline=deadline,
                phases=op.phases,
                resumed_from=ResumedFrom(
                    pid=op.pid, operation=op.operation, started_at=op.started_at
                ),
            )
        )
        return True
    _write(
        LifecycleOp(
            operation=operation,
            pid=os.getpid(),
            started_at=time.monotonic(),
            deadline=deadline,
        )
    )
    return True


@contextmanager
def phase(name: str) -> Generator[None, None, None]:
    """Record one named phase; its outcome is written even when it raises.

    With no journal yet (a caller that skipped begin), an implicit one is
    created so the phase timeline is never lost. Nested phases are preserved:
    the close re-reads the current journal instead of writing the opener's
    snapshot, which would clobber inner phases.
    """
    op = read()
    if op is None:
        op = LifecycleOp(
            operation="unknown",
            pid=os.getpid(),
            started_at=time.monotonic(),
            deadline=None,
        )
    entry = LifecyclePhase(name=name, started_at=time.monotonic())
    op.phases.append(entry)
    op.complete = False
    _write(op)
    try:
        yield
    except BaseException:
        _close_phase(entry, ok=False)
        raise
    _close_phase(entry, ok=True)


def _close_phase(entry: LifecyclePhase, *, ok: bool) -> None:
    """Mark one phase finished on the journal as it stands now."""
    op = read()
    if op is None:
        return
    for candidate in op.phases:
        if candidate.name == entry.name and candidate.started_at == entry.started_at:
            candidate.finished_at = time.monotonic()
            candidate.ok = ok
            _write(op)
            return


def finish(rc: int, *, error: str | None = None, extra: dict[str, Any] | None = None) -> None:
    """Mark the operation complete with its final diagnosis."""
    op = read()
    if op is None:
        return
    op.complete = True
    op.result = {"rc": rc, "error": error, **(extra or {})}
    _write(op)


def read() -> LifecycleOp | None:
    """The current journal, or None when absent or unreadable."""
    try:
        raw = json.loads(_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    data = cast("dict[str, Any]", raw)
    if not isinstance(data.get("operation"), str):
        return None
    raw_phases = data.get("phases", [])
    phases: list[LifecyclePhase] = []
    if isinstance(raw_phases, list):
        for raw in cast("list[object]", raw_phases):
            if not isinstance(raw, dict):
                continue
            entry = cast("dict[str, object]", raw)
            name = entry.get("name")
            if not isinstance(name, str):
                continue
            started = entry.get("started_at", 0.0)
            finished = entry.get("finished_at")
            ok = entry.get("ok")
            phases.append(
                LifecyclePhase(
                    name=name,
                    started_at=float(started) if isinstance(started, (int, float)) else 0.0,
                    finished_at=float(finished) if isinstance(finished, (int, float)) else None,
                    ok=None if ok is None else bool(ok),
                )
            )
    try:
        pid = int(data["pid"])
        started_at = float(data.get("started_at", 0.0))
        deadline = None if data.get("deadline") is None else float(data["deadline"])
    except (KeyError, TypeError, ValueError):
        # A parseable-but-wrong-shape journal (missing identity fields, bad
        # types) is as unreadable as a corrupt one: readers on the
        # stop/pause/restart path must see "absent", never a raise.
        return None
    resumed_from: ResumedFrom | None = None
    raw_resumed = data.get("resumed_from")
    if isinstance(raw_resumed, dict):
        entry = cast("dict[str, object]", raw_resumed)
        prev_pid = entry.get("pid")
        prev_operation = entry.get("operation")
        prev_started = entry.get("started_at")
        if (
            isinstance(prev_pid, int)
            and isinstance(prev_operation, str)
            and isinstance(prev_started, (int, float))
        ):
            resumed_from = ResumedFrom(
                pid=prev_pid, operation=prev_operation, started_at=float(prev_started)
            )
    return LifecycleOp(
        operation=data["operation"],
        pid=pid,
        started_at=started_at,
        deadline=deadline,
        phases=phases,
        complete=bool(data.get("complete", False)),
        result=data.get("result"),
        resumed_from=resumed_from,
    )


def status_path() -> Path:
    """The journal path, for printing the pointer line."""
    return _path()
