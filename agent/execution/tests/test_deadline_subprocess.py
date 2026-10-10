"""Real independent deadline exits, including an orphan executing native code."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psutil
import pytest

from agent.graph.exec.protocol import write_request
from base.agents.incarnation.exec_owner_protocol import (
    OwnerContext,
    OwnerControl,
    publish_owner_message,
)
from base.agents.incarnation.resources import ExecAllocation
from tests.fixtures.pin_agent import exec_context

_PROBE = Path(__file__).with_name("_deadline_probe.py")


def _context(tmp_path: Path, timeout: float, code: str = "pass") -> tuple[OwnerContext, Path]:
    request_id = uuid4()
    request = tmp_path / f"req-{request_id.hex}.json"
    write_request(
        request,
        code=code,
        context=exec_context(None).describe(),
        timeout_s=60,
        state=None,
        incarnation=None,
    )
    context = OwnerContext(
        agent_id=424242,
        generation=uuid4(),
        runtime_owner=uuid4(),
        request_path=request,
        result_path=tmp_path / "result.json",
        allocation=ExecAllocation(
            request=request_id,
            domain=uuid4(),
            request_digest=hashlib.sha256(request.read_bytes()).hexdigest(),
            deadline=datetime.now(UTC) + timedelta(seconds=timeout),
        ),
    )
    path = tmp_path / "owner.json"
    publish_owner_message(path, context)
    return context, path


def _start(context: OwnerContext, path: Path, *, mode: str | None = None) -> psutil.Popen:
    argv = [sys.executable, "-I", "-B"]
    argv += (
        ["-m", "agent.execution.owner_child", "--context", str(path)]
        if mode is None
        else [str(_PROBE), mode, str(path), str(path.with_suffix(".native"))]
    )
    # This launch handle guards cleanup signals against native PID reuse.
    return psutil.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ
        | {
            "AVA_EXEC_REQUEST_FILE": str(context.request_path),
            "AVA_EXEC_RESULT_FILE": str(context.result_path),
        },
    )


def _finish(process: psutil.Popen, permit: OwnerControl | None = None) -> tuple[str, str]:
    try:
        return process.communicate(
            None if permit is None else permit.model_dump_json() + "\n", timeout=20
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def _permit(context: OwnerContext) -> OwnerControl:
    return OwnerControl(
        request=context.allocation.request, domain=context.allocation.domain, action="permit"
    )


def test_normal_entry_collects_watchdog_after_payload_returns(tmp_path: Path) -> None:
    context, path = _context(tmp_path, 10)
    process = _start(context, path, mode="normal")
    stdout, stderr = _finish(process, _permit(context))
    assert process.returncode == 0, stderr
    assert json.loads(stdout) == {"watchdog_live": False}
    assert path.with_suffix(".native").read_text() == "entered"


@pytest.mark.parametrize("failure", ["eof", "digest", "permit"])
def test_refused_gate_collects_watchdog_without_running_payload(
    tmp_path: Path, failure: str
) -> None:
    context, path = _context(tmp_path, 10)
    permit = _permit(context)
    if failure == "digest":
        context.request_path.write_text("private changed request")
    elif failure == "permit":
        permit = OwnerControl(request=uuid4(), domain=context.allocation.domain, action="permit")
    process = _start(context, path, mode="refused")
    stdout, stderr = _finish(process, None if failure == "eof" else permit)
    assert process.returncode == 0, stderr
    receipt = json.loads(stdout)
    assert receipt["watchdog_live"] is False
    assert "exec" in receipt["error"]
    assert not path.with_suffix(".native").exists()


def test_permit_wait_is_independently_bounded(tmp_path: Path) -> None:
    context, path = _context(tmp_path, 3)
    process = _start(context, path)
    started = time.monotonic()
    try:
        # Retain the pipe writer: communicate() would close stdin and test EOF.
        assert process.wait(timeout=5) == 124
        assert time.monotonic() - started < 5
        assert not context.result_path.exists()
    finally:
        _finish(process)


def test_deadline_still_covers_payload_finally(tmp_path: Path) -> None:
    context, path = _context(tmp_path, 3)
    process = _start(context, path, mode="cleanup")
    _stdout, stderr = _finish(process, _permit(context))
    assert process.returncode == 124, stderr
    assert path.with_suffix(".native").exists()


@pytest.mark.parametrize(("mode", "exit_code"), [("expired-stop", 124), ("disarmed", 0)])
def test_stop_deadline_boundary_uses_actual_exit_receipt(
    tmp_path: Path, mode: str, exit_code: int
) -> None:
    context, path = _context(tmp_path, 10)
    process = _start(context, path, mode=mode)
    stdout, stderr = _finish(process)
    assert process.returncode == exit_code, stderr
    if exit_code == 0:
        assert json.loads(stdout) == {"finished": True, "alive": False}


@pytest.mark.skipif(
    sys.platform not in {"linux", "darwin"}, reason="POSIX native sleep/orphan custody"
)
def test_actual_native_child_exits_124_after_its_parent_dies(tmp_path: Path) -> None:
    ready = tmp_path / "owner.native"
    code = (
        "import ctypes, pathlib\n"
        "libc = ctypes.CDLL(None)\n"
        f"pathlib.Path({str(ready)!r}).write_text('native-entry')\n"
        "libc.sleep(60)\n"
        "raise AssertionError('native sleep unexpectedly returned')\n"
    )
    context, path = _context(tmp_path, 10, code)
    process = _start(context, path, mode="orphan")
    started = time.monotonic()
    stdout, stderr = _finish(process)
    assert process.returncode == 0, stderr
    receipt = json.loads(stdout)
    assert receipt["exit_code"] == 124 and receipt["parent_exit"] == -9
    assert receipt["child_birth"] > 0
    assert ready.read_text() == "native-entry"
    assert not context.result_path.exists()
    assert time.monotonic() - started < 15
