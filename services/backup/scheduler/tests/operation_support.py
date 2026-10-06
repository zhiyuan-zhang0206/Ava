"""Private disposable workers for scheduler operation tests."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from services.backup.scheduler.operation import worker_process
from services.backup.scheduler.operation.staging import OperationKind


def operation_kind(tmp_path: Path, **changes: Any) -> OperationKind:
    return OperationKind("test", tmp_path / "controls", **changes)


def stub_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> list[subprocess.Popen[bytes]]:
    script = tmp_path / "worker.py"
    script.write_text("import os,sys,json,time,subprocess\nfrom pathlib import Path\n" + code)
    launch = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def spawn(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        child = cast(
            "subprocess.Popen[bytes]",
            launch([sys.executable, "-I", "-B", str(script), *argv[-2:]], **kwargs),
        )
        children.append(child)
        return child

    monkeypatch.setattr(worker_process.subprocess, "Popen", spawn)
    return children


async def until(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert path.exists(), f"{path.name} was never written"
