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

from shared.log import logger
from shared.paths import run_dir


@dataclass
class LifecyclePhase:
    name: str
    started_at: float
    finished_at: float | None = None
    ok: bool | None = None


@dataclass
class LifecycleOp:
    operation: str
    pid: int
    started_at: float
    deadline: float | None
    phases: list[LifecyclePhase] = field(default_factory=list[LifecyclePhase])
    complete: bool = False
    result: dict[str, Any] | None = None


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


def begin(operation: str, *, deadline: float | None = None) -> bool:
    """Start journaling `operation` (pause / stop / restart).

    Returns True when this call OWNS the journal: it either started a fresh
    one or replaced a completed one. An outer operation's still-running
    journal (a restart wrapping a stop leg) is kept — the inner caller then
    records phases into it but never begins or finishes it.
    """
    op = read()
    if op is not None and not op.complete:
        return False
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
        data = cast("dict[str, Any]", json.loads(_path().read_text()))
    except (OSError, ValueError):
        return None
    if not isinstance(data.get("operation"), str):
        return None
    raw_phases = data.get("phases", [])
    phases: list[LifecyclePhase] = []
    if isinstance(raw_phases, list):
        for raw in raw_phases:
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                continue
            phases.append(
                LifecyclePhase(
                    name=raw["name"],
                    started_at=float(raw.get("started_at", 0.0)),
                    finished_at=(
                        None if raw.get("finished_at") is None else float(raw["finished_at"])
                    ),
                    ok=(None if raw.get("ok") is None else bool(raw["ok"])),
                )
            )
    return LifecycleOp(
        operation=data["operation"],
        pid=int(data["pid"]),
        started_at=float(data.get("started_at", 0.0)),
        deadline=(None if data.get("deadline") is None else float(data["deadline"])),
        phases=phases,
        complete=bool(data.get("complete", False)),
        result=data.get("result"),
    )


def status_path() -> Path:
    """The journal path, for printing the pointer line."""
    return _path()
