"""Real owner/child pipe boundaries, not a mock completion callback."""

import contextlib
import hashlib
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psutil
import pytest

from agent.graph._exec_protocol import read_result, write_request
from shared.exec_owner_protocol import (
    OwnerClosed,
    OwnerContext,
    OwnerControl,
    OwnerReady,
    publish_owner_message,
    read_owner_context,
    validate_native_ready,
)
from shared.incarnation_resources import ExecAllocation


def _context(tmp_path: Path, agent_id: int = 1) -> OwnerContext:
    identity = uuid4()
    request = tmp_path / f"req-{identity.hex}.json"
    write_request(
        request,
        code="raise AssertionError('must not execute without permit')",
        agent_id=agent_id,
        timeout_s=20,
        state=None,
    )
    return OwnerContext(
        agent_id=agent_id,
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
            "-B",
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
        validate_native_ready(
            ready, proc.pid, psutil.Process(proc.pid).create_time(), tmp_path / "owner.json"
        )
        assert ready.allocation.root_process is not None
        root = psutil.Process(ready.allocation.root_process.pid)
        assert root.ppid() == ready.allocation.owner_process.pid
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


def test_completed_owner_exits_while_original_host_keeps_control_open(tmp_path: Path) -> None:
    """Normal completion must not leave a daemon holding buffered stdin at exit."""
    context = _context(tmp_path)
    write_request(
        context.request_path,
        code="import sys; assert sys.dont_write_bytecode",
        agent_id=1,
        timeout_s=20,
        state=None,
    )
    context = context.model_copy(
        update={
            "allocation": context.allocation.model_copy(
                update={
                    "request_digest": hashlib.sha256(context.request_path.read_bytes()).hexdigest(),
                }
            )
        }
    )
    proc = _start(tmp_path, context)
    try:
        ready = _ready(tmp_path, proc)
        assert proc.stdin is not None
        permit = OwnerControl(
            request=ready.allocation.request, domain=ready.allocation.domain, action="permit"
        )
        proc.stdin.write(permit.model_dump_json().encode() + b"\n")
        proc.stdin.flush()
        # Do not close/communicate stdin: the production host keeps it until exit.
        code = proc.wait(timeout=15)
        assert not proc.stdin.closed
        assert proc.stdout is not None
        output = proc.stdout.read()
        assert code == 0, output.decode(errors="replace")
        assert b"Fatal Python error" not in output
        receipt = OwnerClosed.model_validate_json((tmp_path / "owner.closed").read_bytes())
        assert receipt.allocation == ready.allocation
        assert receipt.reason == "completed"
        assert receipt.root_exit_code == 0
        assert context.result_path.exists()
        assert read_result(context.result_path).kind == "done"
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.parametrize("ending", ["record", "partial_eof", "oversize"])
def test_control_pipe_is_bounded_and_owned_by_calling_loop(ending: str) -> None:
    from agent.exec_domain_owner import ControlPipe
    from shared.exec_owner_protocol import MAX_OWNER_MESSAGE

    source, destination = os.pipe()
    try:
        control = ControlPipe(source)
        assert control.read() is None
        os.write(destination, b"part")
        assert control.read() is None
        if ending == "record":
            os.write(destination, b"ial\nnext\n")
            assert control.read() == b"partial\n"
            assert control.read() == b"next\n"
        elif ending == "partial_eof":
            os.close(destination)
            destination = -1
            with pytest.raises(RuntimeError, match="partial record"):
                control.read()
        else:
            # Feed small chunks: the test itself must not fill and block its pipe.
            for _ in range(MAX_OWNER_MESSAGE // 4 - 1):
                os.write(destination, b"more")
                assert control.read() is None
            os.write(destination, b"!")
            with pytest.raises(RuntimeError, match="exceeds"):
                control.read()
    finally:
        os.close(source)
        if destination >= 0:
            os.close(destination)


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


def test_owner_death_is_not_a_terminal_receipt(tmp_path: Path) -> None:
    context = _context(tmp_path)
    proc = _start(tmp_path, context)
    try:
        ready = _ready(tmp_path, proc)
        assert ready.allocation.owner_process is not None
        owner = psutil.Process(ready.allocation.owner_process.pid)
        assert owner.create_time() == ready.allocation.owner_process.birth
        owner.kill()
        proc.wait(timeout=10)
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


def test_owner_import_does_not_boot_graph_sdk_or_settings(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sys; import agent.exec_domain_owner; "
            "assert 'agent.graph' not in sys.modules; assert 'ava' not in sys.modules; "
            "assert 'shared.config' not in sys.modules",
        ],
        cwd=tmp_path,
        env=dict(os.environ, AVA_AGENT_ID="1", AVA_PROCESS_PROFILE="agent"),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


_HOST = """
import os, subprocess, sys, time
from pathlib import Path
from shared.exec_owner_protocol import OwnerControl, OwnerReady, read_owner_context
path = Path(sys.argv[1])
context = read_owner_context(path)
owner = subprocess.Popen(
    [sys.executable, '-I', '-B', '-X', 'utf8', '-m', 'agent.exec_domain_owner', '--context', str(path)],
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    close_fds=True,
)
while not path.with_suffix('.ready').exists():
    if owner.poll() is not None:
        raise RuntimeError('owner exited before handshake')
    time.sleep(.01)
ready = OwnerReady.model_validate_json(path.with_suffix('.ready').read_bytes())
owner.stdin.write(OwnerControl(request=ready.allocation.request,
    domain=ready.allocation.domain, action='permit').model_dump_json().encode()+b'\\n')
owner.stdin.flush()
while True:
    time.sleep(1)
"""


def test_real_host_death_closes_active_managed_child(
    tmp_path: Path, record_property: Callable[[str, object], None]
) -> None:
    """A separate host dies; its child cannot keep the control writer alive."""
    context = _context(tmp_path)
    active = tmp_path / "active"
    code = (
        "import os, time\nfrom pathlib import Path\n"
        f"Path({str(active)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    write_request(context.request_path, code=code, agent_id=1, timeout_s=20, state=None)
    context = context.model_copy(
        update={
            "allocation": context.allocation.model_copy(
                update={
                    "request_digest": hashlib.sha256(context.request_path.read_bytes()).hexdigest()
                }
            )
        }
    )
    path = tmp_path / "owner.json"
    publish_owner_message(path, context)
    started = time.monotonic()
    host = subprocess.Popen(  # noqa: S603 -- disposable fixed host, never a service.
        [sys.executable, "-I", "-B", "-c", _HOST, str(path)],
        cwd=tmp_path,
        env=dict(
            os.environ,
            AVA_EXEC_REQUEST_FILE=str(context.request_path),
            AVA_EXEC_RESULT_FILE=str(context.result_path),
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    ready: OwnerReady | None = None
    try:
        while not active.exists():
            if host.poll() is not None or time.monotonic() - started > 18:
                raise AssertionError("actual exec never reached user code")
            time.sleep(0.02)
        ready = OwnerReady.model_validate_json(path.with_suffix(".ready").read_bytes())
        assert ready.allocation.owner_process is not None
        assert ready.allocation.root_process is not None
        owner = psutil.Process(ready.allocation.owner_process.pid)
        assert owner.create_time() == ready.allocation.owner_process.birth
        record_property("owner_cold_start_ms", (time.monotonic() - started) * 1000)
        record_property("owner_rss_bytes", owner.memory_info().rss)
        user = psutil.Process(int(active.read_text()))
        user_birth = user.create_time()
        host.kill()
        host.wait(timeout=5)
        until = time.monotonic() + 10
        while not path.with_suffix(".closed").exists():
            if time.monotonic() >= until:
                raise AssertionError("dead host did not produce positive domain closure")
            time.sleep(0.02)
        receipt = OwnerClosed.model_validate_json(path.with_suffix(".closed").read_bytes())
        assert receipt.allocation == ready.allocation
        assert receipt.reason == "host_eof"
        with contextlib.suppress(psutil.NoSuchProcess):
            assert user.create_time() != user_birth or user.status() == psutil.STATUS_ZOMBIE
    finally:
        if host.poll() is None:
            host.kill()
            host.wait(timeout=5)
        # The original deadline still bounds owner/root on assertion failure.
        # Never kill a re-resolved PID or turn cleanup into a passing receipt.
