"""Deterministic unit tests for disposable exec process ownership."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import psutil
import pytest

from agent.graph._exec_process import (
    _READER_JOIN_TIMEOUT_S,
    DomainCloseOwner,
    ExecProcessDomain,
    ExecTeardownError,
    TeardownFailure,
    annotate_original_failure,
    finish_teardown_despite_cancellation,
    settle_cancelled_owners,
    settle_resources,
    signal_child,
    start_reader_join,
    start_reap,
    start_root_exit_observer,
    wait_with_grace,
)
from agent.graph._exec_stream import StreamingTextIO
from agent.graph._exec_subprocess import _collect_child, _spawn

_AGENT_ID = 424242


async def _assert_tree_gone(pids: list[int], timeout_s: float = 5.0) -> None:
    """Assert no live member of the process-ids after teardown.

    A SIGKILLed descendant can remain a zombie until its new parent reaps it,
    and psutil.pid_exists() still reports those entries — a snapshot check
    races the OS reaper (same discipline as
    tests/agent/test_exec_subprocess.py::_assert_tree_gone and
    tests/services/test_pitr_base_scheduler.py::_assert_tree_gone). A zombie
    or already-reaped pid counts as gone.
    """
    deadline = time.monotonic() + timeout_s
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        for pid in list(remaining):
            try:
                if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                    remaining.discard(pid)
            except psutil.NoSuchProcess:
                remaining.discard(pid)
        if remaining:
            await asyncio.sleep(0.05)
    assert not remaining, f"process(es) still alive after exec teardown: {sorted(remaining)}"


async def _assert_group_gone(pgid: int, timeout_s: float = 5.0) -> None:
    """Assert the process group's member table is empty after teardown.

    ``killpg(pgid, 0)`` keeps succeeding while any member — including a
    zombie awaiting its reaper — remains in the group table, so a one-shot
    ``ProcessLookupError`` expectation races the OS reaper. Poll until ESRCH;
    reaping completes well within the bound.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.05)
    raise AssertionError(f"process group {pgid} still present after exec teardown")


async def test_grace_expiry_waits_on_popen_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reap wait survives the grace timeout; starting a second wait races
    two waitpid calls over the same direct child."""

    class _BlockingProc:
        pid = 12345

        def __init__(self) -> None:
            self.wait_calls = 0
            self.release = threading.Event()

        def wait(self) -> int:
            self.wait_calls += 1
            assert self.release.wait(timeout=5.0)
            return -signal.SIGKILL

    proc = _BlockingProc()

    root_exited = threading.Event()

    async def _observe_root() -> None:
        await asyncio.to_thread(root_exited.wait)

    root_exit_task = asyncio.create_task(_observe_root())

    class _Domain:
        def __init__(self) -> None:
            self.proc = proc
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            proc.release.set()
            root_exited.set()

    domain = _Domain()
    domain_close = DomainCloseOwner(domain, root_exit_task)  # type: ignore[arg-type]
    reap_task = start_reap(proc, domain_close)  # type: ignore[arg-type]
    assert await wait_with_grace(proc, root_exit_task, 0.01, domain_close) is False  # type: ignore[arg-type]
    assert not await settle_resources(
        root_exit_task, reap_task, domain_close, None, request_stop=False
    )
    assert proc.wait_calls == 1
    assert domain.close_calls == 1


async def test_reader_join_uses_its_own_bound() -> None:
    """A descendant holding stdout open must not strand an executor worker in
    an unbounded ``reader.join`` after the asyncio timeout has fired."""

    class _ExitedProc:
        pid = 54321

        def wait(self) -> int:
            return 0

    class _Reader:
        def __init__(self) -> None:
            self.timeouts: list[float | None] = []

        def join(self, timeout: float | None = None) -> None:
            self.timeouts.append(timeout)

        def is_alive(self) -> bool:
            return False

    reader = _Reader()
    proc = _ExitedProc()
    root_exit_task = asyncio.create_task(asyncio.sleep(0))

    class _Domain:
        def __init__(self) -> None:
            self.proc = proc

        def close(self) -> None:
            return

    domain_close = DomainCloseOwner(_Domain(), root_exit_task)  # type: ignore[arg-type]
    reap_task = start_reap(proc, domain_close)  # type: ignore[arg-type]
    reader_join_task = start_reader_join(
        reap_task,
        reader,  # type: ignore[arg-type]
        proc.pid,
    )
    await _collect_child(
        proc,  # type: ignore[arg-type]
        StreamingTextIO(),
        None,
        cancelled=False,
        timed_out=False,
        root_exit_task=root_exit_task,
        reap_task=reap_task,
        domain_close=domain_close,
        reader_join_task=reader_join_task,
    )
    assert reader.timeouts == [_READER_JOIN_TIMEOUT_S]


async def test_reader_join_fails_loud_when_pipe_never_reaches_eof() -> None:
    """A timed join is not success: an alive reader is an explicit resource
    cleanup failure rather than a silently completed barrier."""

    class _Reader:
        def join(self, _timeout: float | None = None) -> None:
            return

        def is_alive(self) -> bool:
            return True

    reap_task = asyncio.create_task(asyncio.sleep(0, result=0))

    task = start_reader_join(
        reap_task,
        _Reader(),  # type: ignore[arg-type]
        999,
    )
    with pytest.raises(RuntimeError, match="remained alive"):
        await task


async def test_cleanup_failures_do_not_short_circuit_reap_or_reader() -> None:
    """Close and reap failures are both observed, and the bounded reader join
    still runs; returned failure order is stable ownership→reap→reader."""
    events: list[str] = []

    class _Proc:
        pid = 111

        def wait(self) -> int:
            events.append("reap")
            raise OSError("wait failed")

    class _Domain:
        proc = _Proc()

        def close(self) -> None:
            events.append("close")
            raise OSError("close failed")

    class _Reader:
        def join(self, timeout: float | None = None) -> None:
            assert timeout == _READER_JOIN_TIMEOUT_S
            events.append("reader")

        def is_alive(self) -> bool:
            return True

    root_exit_task = asyncio.create_task(asyncio.sleep(0))
    domain_close = DomainCloseOwner(_Domain(), root_exit_task)  # type: ignore[arg-type]
    reap_task = start_reap(_Domain.proc, domain_close)  # type: ignore[arg-type]
    reader_join_task = start_reader_join(
        reap_task,
        _Reader(),  # type: ignore[arg-type]
        _Domain.proc.pid,  # type: ignore[arg-type]
    )

    failures = await settle_resources(
        root_exit_task,
        reap_task,
        domain_close,
        reader_join_task,
        request_stop=False,
    )

    assert [failure.stage for failure in failures] == [
        "domain_close",
        "reap",
        "reader_join",
    ]
    assert events == ["close", "reap", "reader"]


async def test_posix_domain_closes_before_the_only_popen_wait() -> None:
    """The zombie root pins its pid/pgid until group close; only then is the
    direct child reaped, preventing a late numeric process-group lookup."""
    events: list[str] = []

    class _Proc:
        pid = 222

        def wait(self) -> int:
            events.append("reap")
            return 0

    class _Domain:
        proc = _Proc()

        def close(self) -> None:
            events.append("close")

    root_exit_task = asyncio.create_task(asyncio.sleep(0))
    domain_close = DomainCloseOwner(_Domain(), root_exit_task)  # type: ignore[arg-type]
    reap_task = start_reap(_Domain.proc, domain_close)  # type: ignore[arg-type]

    assert not await settle_resources(
        root_exit_task, reap_task, domain_close, None, request_stop=False
    )
    assert events == ["close", "reap"]


def test_original_failure_stays_primary_when_teardown_also_fails() -> None:
    original = ValueError("work failed")
    cleanup = OSError("close failed")
    failures = (TeardownFailure("domain_close", cleanup),)

    annotate_original_failure(original, failures)

    assert isinstance(original, ValueError)
    assert original.__notes__ == [str(ExecTeardownError(failures))]


def test_zombie_only_group_eperm_is_a_verified_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = MagicMock(pid=444)
    live_checks = iter([True, False])

    def _has_live_member(_pgid: int) -> bool:
        return next(live_checks)

    monkeypatch.setattr("agent.graph._exec_process.IS_WINDOWS", False)
    monkeypatch.setattr(
        "agent.graph._exec_process._process_group_has_live_member",
        _has_live_member,
    )
    monkeypatch.setattr(
        "agent.graph._exec_process.os.killpg",
        MagicMock(side_effect=PermissionError),
    )

    ExecProcessDomain(proc=proc, windows_job=None).close()

    proc.kill.assert_not_called()


async def test_dead_status_is_a_terminal_non_reaping_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = MagicMock()
    identity.status.return_value = psutil.STATUS_DEAD
    proc = MagicMock(pid=555)

    def _identity_for_pid(_pid: int) -> MagicMock:
        return identity

    monkeypatch.setattr("agent.graph._exec_process.IS_WINDOWS", False)
    monkeypatch.setattr("agent.graph._exec_process.psutil.Process", _identity_for_pid)

    await asyncio.wait_for(start_root_exit_observer(proc), timeout=1.0)

    proc.poll.assert_not_called()


async def test_missing_process_is_a_terminal_non_reaping_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = MagicMock()
    identity.status.side_effect = psutil.NoSuchProcess(pid=556)
    proc = MagicMock(pid=556)

    monkeypatch.setattr("agent.graph._exec_process.IS_WINDOWS", False)
    monkeypatch.setattr(
        "agent.graph._exec_process.psutil.Process", MagicMock(return_value=identity)
    )

    await asyncio.wait_for(start_root_exit_observer(proc), timeout=1.0)

    proc.poll.assert_not_called()


async def test_repeated_cancellation_cannot_interrupt_resource_barrier() -> None:
    """A second cancellation during cleanup is consumed; after every owner
    settles, the outer operation still re-raises its original CancelledError."""
    events: list[str] = []
    root_exited = threading.Event()
    reap_release = asyncio.Event()
    reap_started = asyncio.Event()
    operation_started = asyncio.Event()
    cleanup_started = asyncio.Event()

    async def _observe_root() -> None:
        await asyncio.to_thread(root_exited.wait)

    root_exit_task = asyncio.create_task(_observe_root())

    class _Proc:
        pid = 333

    class _Domain:
        proc = _Proc()

        def close(self) -> None:
            events.append("close")
            root_exited.set()

    domain_close = DomainCloseOwner(_Domain(), root_exit_task)  # type: ignore[arg-type]

    async def _reap() -> int:
        with contextlib.suppress(Exception):
            await domain_close.wait()
        events.append("reap")
        reap_started.set()
        await reap_release.wait()
        return 0

    reap_task = asyncio.create_task(_reap())

    async def _join_reader() -> None:
        with contextlib.suppress(Exception):
            await asyncio.shield(reap_task)
        events.append("reader")

    reader_join_task = asyncio.create_task(_join_reader())

    async def _operation() -> None:
        try:
            operation_started.set()
            await asyncio.Future()
        except asyncio.CancelledError:
            cleanup_started.set()
            await finish_teardown_despite_cancellation(
                root_exit_task, reap_task, domain_close, reader_join_task
            )
            raise

    operation = asyncio.create_task(_operation())
    await asyncio.wait_for(operation_started.wait(), timeout=1.0)
    operation.cancel()
    await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)
    await asyncio.wait_for(reap_started.wait(), timeout=1.0)
    operation.cancel()
    operation.cancel()
    reap_release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, timeout=5.0)
    assert events == ["close", "reap", "reader"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group regression")
async def test_runner_cancelled_owners_leave_no_exec_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Runner-wide cancellation must not strand the exec child or an observer
    thread that makes ``shutdown_default_executor`` wait for that child."""
    descendant_pid_path = tmp_path / "descendant.pid"
    code = (
        "import subprocess, sys, time; "
        f"p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(descendant_pid_path)!r}, 'w').write(str(p.pid)); "
        "time.sleep(60)"
    )
    proc = subprocess.Popen(  # noqa: S603 — fixed test-only interpreter command
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert proc.stdout is not None
    domain = ExecProcessDomain(proc=proc, windows_job=None)
    root_exit_task = start_root_exit_observer(proc)
    domain_close = DomainCloseOwner(domain, root_exit_task)
    reap_task = start_reap(proc, domain_close)
    reader = threading.Thread(target=proc.stdout.read, daemon=True)
    reader.start()
    reader_join_task = start_reader_join(reap_task, reader, proc.pid)
    try:
        deadline = time.monotonic() + 5.0
        while not descendant_pid_path.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        descendant_pid = int(descendant_pid_path.read_text())

        for task in (domain_close.task, root_exit_task, reap_task, reader_join_task):
            task.cancel()
        await asyncio.gather(
            domain_close.task,
            root_exit_task,
            reap_task,
            reader_join_task,
            return_exceptions=True,
        )
        assert domain_close.task.cancelled()

        # The emergency path must not enqueue new default-executor work: that
        # executor is exactly what Runner is trying to shut down in production.
        monkeypatch.setattr(
            "agent.graph._exec_process.asyncio.to_thread",
            MagicMock(side_effect=AssertionError("default executor re-entered")),
        )
        started = time.monotonic()
        assert not settle_cancelled_owners(domain_close, reader)
        assert time.monotonic() - started < 6.0

        assert proc.poll() is not None
        await _assert_tree_gone([descendant_pid])
        await _assert_group_gone(proc.pid)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5.0)


async def test_windows_stop_closes_the_owned_job_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows stops through the root-independent Job handle, never Popen."""
    proc = MagicMock(pid=777)
    job = MagicMock()
    root_exit_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(60))
    domain = MagicMock(proc=proc, windows_job=job)
    domain.close.side_effect = job.close
    domain_close = DomainCloseOwner(domain, root_exit_task)
    monkeypatch.setattr("agent.graph._exec_process.IS_WINDOWS", True)

    signal_child(proc, signal.SIGTERM, domain_close)
    await domain_close.wait()
    domain_close.request()
    await domain_close.wait()

    job.close.assert_called_once_with()
    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()
    root_exit_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await root_exit_task


def test_windows_job_attach_failure_kills_reaps_and_closes_pipe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Attach stays primary while every failed cleanup stage remains visible."""

    class _FailingJob:
        def __init__(self) -> None:
            self.close_calls = 0
            self.assign_calls = 0

        def assign(self, _proc: object) -> None:
            self.assign_calls += 1
            raise OSError("attach failed")

        def close(self) -> None:
            self.close_calls += 1
            raise OSError("close failed")

    class _SpawnedProc:
        pid = 888

        def __init__(self) -> None:
            self.stdout = io.BytesIO()
            self.kill_calls = 0
            self.wait_calls = 0

        def kill(self) -> None:
            self.kill_calls += 1
            raise OSError("kill failed")

        def wait(self, timeout: float | None = None) -> int:
            assert timeout == 5.0
            self.wait_calls += 1
            assert timeout is not None
            raise subprocess.TimeoutExpired("exec child", timeout)

    job = _FailingJob()
    proc = _SpawnedProc()
    popen_env: dict[str, str] = {}

    def _fake_popen(*_args: object, **kwargs: object) -> _SpawnedProc:
        popen_env.update(kwargs["env"])  # type: ignore[arg-type]
        return proc

    monkeypatch.setattr("agent.graph._exec_subprocess.IS_WINDOWS", True)
    monkeypatch.setattr("agent.graph._exec_subprocess.WindowsJob.create", lambda: job)
    monkeypatch.setattr("agent.graph._exec_subprocess.subprocess.Popen", _fake_popen)
    gate = tmp_path / "attach.job-ready"

    with pytest.raises(OSError, match="attach failed") as caught:
        _spawn(
            tmp_path / "request.json",
            tmp_path / "result.json",
            _AGENT_ID,
            windows_job_gate=gate,
        )

    assert job.assign_calls == 1
    assert job.close_calls == 1
    assert proc.kill_calls == 1
    assert proc.wait_calls == 1
    assert proc.stdout.closed
    assert popen_env["AVA_EXEC_JOB_GATE"] == str(gate)
    notes = getattr(caught.value, "__notes__", ())
    assert any("job_close: OSError: close failed" in note for note in notes)
    assert any("root_kill: OSError: kill failed" in note for note in notes)
    assert any("root_reap: TimeoutExpired" in note for note in notes)


def test_windows_popen_failure_preserves_primary_when_job_close_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Job cleanup diagnostics cannot replace the original Popen failure."""

    class _FailingJob:
        def close(self) -> None:
            raise OSError("close failed")

    def _failing_popen(*_args: object, **_kwargs: object) -> None:
        raise OSError("spawn failed")

    job = _FailingJob()
    monkeypatch.setattr("agent.graph._exec_subprocess.IS_WINDOWS", True)
    monkeypatch.setattr("agent.graph._exec_subprocess.WindowsJob.create", lambda: job)
    monkeypatch.setattr("agent.graph._exec_subprocess.subprocess.Popen", _failing_popen)

    with pytest.raises(OSError, match="spawn failed") as caught:
        _spawn(
            tmp_path / "request.json",
            tmp_path / "result.json",
            _AGENT_ID,
            windows_job_gate=tmp_path / "attach.job-ready",
        )

    assert any(
        "job_close: OSError: close failed" in note
        for note in getattr(caught.value, "__notes__", ())
    )
