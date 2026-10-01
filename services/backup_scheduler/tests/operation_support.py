"""Fixtures and helpers shared by the operation custody tests.

The custody tests drive real worker processes: `stub_worker` swaps the launch of
the operation worker for a small script, so a test can make the worker fork,
hang, ignore signals or write a result without importing any backup code. The
helpers lay control directories out the way an earlier controller left them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psutil
import pytest

from base.native_process import native_boot_id
from base.native_process.exec_domain import ExecProcessDomain
from services.backup_scheduler.operation import custody
from services.backup_scheduler.operation.custody import NativeProcess, OperationKind


@pytest.fixture(autouse=True)
def release_held() -> Iterator[None]:
    """Unresolved leaders are process-lifetime state; close them per test."""
    yield
    with custody._HELD_LOCK:
        held = list(custody._HELD)
        custody._HELD.clear()
    for item in held:
        if item.process.returncode is None:
            with contextlib.suppress(Exception):
                os.killpg(item.process.pid, signal.SIGKILL)
            item.process.wait(timeout=5)


def operation_kind(tmp_path: Path, **changes: Any) -> OperationKind:
    """An operation kind whose control and quarantine roots live under `tmp_path`."""
    return OperationKind("test", tmp_path / "controls", tmp_path / "quarantine", **changes)


def entries(tmp_path: Path, kind: str | None = None) -> list[Path]:
    """Quarantined operations of the test kind, or of one production kind."""
    root = tmp_path / "quarantine"
    return custody.quarantine_entries(root if kind is None else root / kind)


def control_dir(tmp_path: Path, name: str, **records: object) -> Path:
    """One `.operation-*` control directory holding the named record files."""
    work = tmp_path / "controls" / f".operation-{name}"
    work.mkdir(parents=True)
    (work / "operation.json").write_text(
        json.dumps({"kind": "test", "module": "m", "boot_id": native_boot_id(), "at": "t"})
    )
    for record, value in records.items():
        (work / f"{record}.json").write_text(json.dumps(value))
    return work


def exited_worker() -> dict[str, object]:
    """A real worker that exited and was reaped: its group is empty."""
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    native = NativeProcess.capture(psutil.Process(process.pid))
    process.wait(timeout=10)
    return {"pid": process.pid, "native": native.value()}


def stub_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> list[tuple[subprocess.Popen[bytes], ExecProcessDomain]]:
    """Launch `code` instead of the operation worker; returns the launched children."""
    script = tmp_path / "worker.py"
    script.write_text("import os,sys,json,time,subprocess\nfrom pathlib import Path\n" + code)
    launch = ExecProcessDomain.launch_posix
    children: list[tuple[subprocess.Popen[bytes], ExecProcessDomain]] = []

    def spawn(argv: list[str], **kwargs: Any):
        child = launch([sys.executable, "-I", "-B", str(script), *argv[-2:]], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(ExecProcessDomain, "launch_posix", spawn)
    return children


async def until(path: Path) -> None:
    """Wait (bounded) for the stub worker to create `path`."""
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert path.exists(), f"{path.name} was never written"
