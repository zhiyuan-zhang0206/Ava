"""Real owner/child pipe boundaries, not a mock completion callback."""

import hashlib
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psutil
import pytest

from agent.graph._exec_protocol import write_request
from shared.exec_owner_protocol import (
    OwnerClosed,
    OwnerContext,
    OwnerControl,
    OwnerReady,
    publish_owner_message,
    read_owner_context,
)
from shared.incarnation_resources import ExecAllocation


def _context(tmp_path: Path) -> OwnerContext:
    identity = uuid4()
    request = tmp_path / f"req-{identity.hex}.json"
    write_request(
        request,
        code="raise AssertionError('must not execute without permit')",
        agent_id=1,
        timeout_s=20,
        state=None,
    )
    return OwnerContext(
        agent_id=1,
        generation=uuid4(),
        runtime_owner=uuid4(),
        request_path=request.resolve(),
        result_path=(tmp_path / "result.json").resolve(),
        allocation=ExecAllocation(
            request=identity,
            domain=uuid4(),
            request_digest=hashlib.sha256(request.read_bytes()).hexdigest(),
            deadline=datetime.now(UTC) + timedelta(seconds=20),
        ),
    )


def _start(tmp_path: Path, context: OwnerContext) -> subprocess.Popen[bytes]:
    path = tmp_path / "owner.json"
    publish_owner_message(path, context)
    env = dict(
        os.environ,
        AVA_EXEC_REQUEST_FILE=str(context.request_path),
        AVA_EXEC_RESULT_FILE=str(context.result_path),
    )
    return subprocess.Popen(  # noqa: S603 -- fixed isolated owner entry in a disposable fixture.
        [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            "-m",
            "agent.exec_domain_owner",
            "--context",
            str(path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        close_fds=True,
        cwd=tmp_path,
    )


def _ready(tmp_path: Path, proc: subprocess.Popen[bytes]) -> OwnerReady:
    until = time.monotonic() + 15
    path = tmp_path / "owner.ready"
    while not path.exists():
        if proc.poll() is not None or time.monotonic() >= until:
            raise AssertionError(f"owner failed before ready: {proc.communicate(timeout=2)}")
        time.sleep(0.02)
    return OwnerReady.model_validate_json(path.read_bytes())


def test_eof_closes_exact_domain_before_user_code(tmp_path: Path) -> None:
    context = _context(tmp_path)
    proc = _start(tmp_path, context)
    try:
        ready = _ready(tmp_path, proc)
        assert ready.allocation.owner_process is not None
        assert ready.allocation.owner_process.pid == proc.pid
        assert ready.allocation.root_process is not None
        root = psutil.Process(ready.allocation.root_process.pid)
        assert root.ppid() == proc.pid
        assert root.create_time() == ready.allocation.root_process.birth
        assert not context.result_path.exists()
        assert proc.stdin is not None
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0
        receipt = OwnerClosed.model_validate_json((tmp_path / "owner.closed").read_bytes())
        assert receipt.reason == "host_eof"
        assert receipt.allocation == ready.allocation
        assert not context.result_path.exists()
        assert not root.is_running() or root.status() == psutil.STATUS_ZOMBIE
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_wrong_permit_never_publishes_success(tmp_path: Path) -> None:
    context = _context(tmp_path)
    proc = _start(tmp_path, context)
    try:
        _ready(tmp_path, proc)
        assert proc.stdin is not None
        wrong = OwnerControl(request=uuid4(), domain=context.allocation.domain, action="permit")
        proc.stdin.write(wrong.model_dump_json().encode() + b"\n")
        proc.stdin.flush()
        assert proc.wait(timeout=10) != 0
        assert not (tmp_path / "owner.closed").exists()
        assert not context.result_path.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_context_unknown_fields_and_symlink_refuse(tmp_path: Path) -> None:
    context = _context(tmp_path)
    path = tmp_path / "owner.json"
    publish_owner_message(path, context)
    with pytest.raises(FileExistsError):
        publish_owner_message(path, context)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(path)
    except OSError:
        pytest.skip("native symlink privilege unavailable")
    with pytest.raises(ValueError, match="canonical"):
        read_owner_context(alias)
