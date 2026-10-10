"""Deterministic unit tests for disposable exec process ownership."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import psutil
import pytest

from agent.graph.exec import _process, _subprocess
from agent.graph.exec._output_pipe import ExecOutputPipe
from agent.graph.exec._process import (
    _READER_JOIN_TIMEOUT_S,
    DomainCloseOwner,
    ExecProcessDomain,
    ExecTeardownError,
    TeardownFailure,
    annotate_original_failure,
    finish_teardown_despite_cancellation,
    observe_root_exit,
    settle_cancelled_owners,
    settle_resources,
    start_reader_join,
    start_reap,
    wait_with_grace,
)
from agent.graph.exec._result import _ExecCrashed
from agent.graph.exec._stream import StreamingTextIO
from agent.graph.exec._subprocess import _collect_child
from base.db import Database
from base.native_process.ownership import OwnedProcess
from base.native_process.turn_identity import HostedServiceResources, HostedTurnResources
from base.sessions.posixproc import process_group_has_live_members
from tests.e2e.process_support import kill_group_or_prove_already_gone
from tests.fixtures.pin_agent import exec_context

_AGENT_ID = 424242


async def _assert_tree_gone(pids: list[int], timeout_s: float = 5.0) -> None:
    """Assert no live member of the process-ids after teardown.

    A SIGKILLed descendant can remain a zombie until its new parent reaps it,
    and psutil.pid_exists() still reports those entries — a snapshot check
    races the OS reaper (same discipline as
    agent/tests/test_exec_subprocess.py::_assert_tree_gone). A zombie
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
    """Observe no live member in the numeric process group after teardown.

    ``killpg(pgid, 0)`` keeps succeeding while any member — including a
    zombie awaiting its reaper — remains in the group table, so a one-shot
    ``ProcessLookupError`` expectation races the OS reaper; poll instead.
    ``process_group_has_live_members`` is the session supervisor's observation.
    macOS answers a zombie-only group's ``killpg(pgid, 0)`` with EPERM, not ESRCH,
    so the observation scans members rather than relying on that signal probe.
    Native domain closure is established separately by its retained leader.
    """
    deadline = time.monotonic() + timeout_s
    while process_group_has_live_members(pgid):
        if time.monotonic() >= deadline:
            raise AssertionError(f"process group {pgid} still present after exec teardown")
        await asyncio.sleep(0.05)


async def test_grace_expiry_waits_on_popen_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reap wait survives the grace timeout; starting a second wait races
    two waitpid calls over the same direct child."""

    class _BlockingProc:
        pid = 12345

        def __init__(self) -> None:
            self.wait_calls = 0
            self.release = threading.Event()

        def wait(self, timeout: float | None = None) -> int:
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

        def close_confirmed(self, _deadline: float) -> None:
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
    """The reap barrier gives the output tail its own finite EOF budget."""

    class _ExitedProc:
        pid = 54321

        def wait(self, timeout: float | None = None) -> int:
            return 0

    class _Reader:
        def __init__(self) -> None:
            self.timeouts: list[float | None] = []

        async def finish(self, timeout: float) -> None:
            self.timeouts.append(timeout)

        closed = True

    reader = _Reader()
    proc = _ExitedProc()
    root_exit_task = asyncio.create_task(asyncio.sleep(0))

    class _Domain:
        def __init__(self) -> None:
            self.proc = proc

        def close_confirmed(self, _deadline: float) -> None:
            return

    domain_close = DomainCloseOwner(_Domain(), root_exit_task)  # type: ignore[arg-type]
    reap_task = start_reap(proc, domain_close)  # type: ignore[arg-type]
    reader_join_task = start_reader_join(
        reap_task,
        reader,  # type: ignore[arg-type]
        domain_close,
    )
    await _collect_child(
        proc,  # type: ignore[arg-type]
        StreamingTextIO(max_chars=1_000_000),
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
        async def finish(self, _timeout: float) -> None:
            return

        closed = False

    reap_task = asyncio.create_task(asyncio.sleep(0, result=0))
    domain = MagicMock(proc=MagicMock(pid=999))
    owner = DomainCloseOwner(domain, asyncio.create_task(asyncio.sleep(0)))

    task = start_reader_join(
        reap_task,
        _Reader(),  # type: ignore[arg-type]
        owner,
    )
    with pytest.raises(RuntimeError, match="remained alive"):
        await task
    await asyncio.gather(owner.task, owner.root_exit_task)


async def test_cleanup_failure_retains_leader_and_still_joins_reader() -> None:
    """Failed closure blocks reap while the bounded reader join still runs."""
    events: list[str] = []

    class _Proc:
        pid = 111

        def wait(self, timeout: float | None = None) -> int:
            events.append("reap")
            raise OSError("wait failed")

    class _Domain:
        proc = _Proc()

        def close_confirmed(self, _deadline: float) -> None:
            events.append("close")
            raise OSError("close failed")

    class _Reader:
        async def finish(self, timeout: float) -> None:
            assert timeout == _READER_JOIN_TIMEOUT_S
            events.append("reader")

        closed = False

    root_exit_task = asyncio.create_task(asyncio.sleep(0))
    domain_close = DomainCloseOwner(_Domain(), root_exit_task)  # type: ignore[arg-type]
    reap_task = start_reap(_Domain.proc, domain_close)  # type: ignore[arg-type]
    reader_join_task = start_reader_join(
        reap_task,
        _Reader(),  # type: ignore[arg-type]
        domain_close,
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
    assert events == ["close", "reader"]


async def test_posix_domain_closes_before_the_only_popen_wait() -> None:
    """The zombie root pins its pid/pgid until group close; only then is the
    direct child reaped, preventing a late numeric process-group lookup."""
    events: list[str] = []

    class _Proc:
        pid = 222

        def wait(self, timeout: float | None = None) -> int:
            events.append("reap")
            return 0

    class _Domain:
        proc = _Proc()

        def close_confirmed(self, _deadline: float) -> None:
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


async def test_dead_status_is_a_terminal_non_reaping_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = MagicMock()
    identity.status.return_value = psutil.STATUS_DEAD
    proc = MagicMock(pid=555)

    def _identity_for_pid(_pid: int) -> MagicMock:
        return identity

    monkeypatch.setattr("agent.graph.exec._process.psutil.Process", _identity_for_pid)

    await asyncio.wait_for(observe_root_exit(proc), timeout=1.0)

    proc.poll.assert_not_called()


async def test_missing_process_is_a_terminal_non_reaping_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = MagicMock()
    identity.status.side_effect = psutil.NoSuchProcess(pid=556)
    proc = MagicMock(pid=556)

    monkeypatch.setattr(
        "agent.graph.exec._process.psutil.Process", MagicMock(return_value=identity)
    )

    await asyncio.wait_for(observe_root_exit(proc), timeout=1.0)

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

        def close_confirmed(self, _deadline: float) -> None:
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


async def test_runner_cancelled_owners_leave_no_exec_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Runner-wide cancellation must not strand the exec child or an observer
    thread that makes ``shutdown_default_executor`` wait for that child."""
    descendant_pid_path = tmp_path / "descendant.pid"
    temporary_pid_path = tmp_path / "descendant.pid.tmp"
    code = (
        "import os, subprocess, sys, time\n"
        "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"with open({str(temporary_pid_path)!r}, 'w') as ready:\n"
        "    ready.write(str(p.pid))\n"
        f"os.replace({str(temporary_pid_path)!r}, {str(descendant_pid_path)!r})\n"
        "time.sleep(60)"
    )
    proc, domain = ExecProcessDomain.launch_posix(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        new_session=True,
    )
    assert proc.stdout is not None
    domain_close = DomainCloseOwner(domain)
    root_exit_task = domain_close.root_exit_task
    reap_task = start_reap(proc, domain_close)
    reader = ExecOutputPipe(proc, StreamingTextIO(max_chars=1_000_000))
    reader_join_task = start_reader_join(reap_task, reader, domain_close)
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
            "agent.graph.exec._process.asyncio.to_thread",
            MagicMock(side_effect=AssertionError("default executor re-entered")),
        )
        started = time.monotonic()
        assert not settle_cancelled_owners(domain_close, reader)
        assert time.monotonic() - started < 6.0

        assert proc.poll() is not None
        await _assert_tree_gone([descendant_pid])
        await _assert_group_gone(proc.pid)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError) as exc:
            kill_group_or_prove_already_gone(proc, exc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5.0)


async def test_live_signal_refusal_returns_unresolved_without_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno

    from base.native_process.ownership import OwnedProcess

    proc, domain = ExecProcessDomain.launch_posix(
        [sys.executable, "-I", "-c", "import time;time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    native = OwnedProcess.capture(psutil.Process(proc.pid))
    closer = DomainCloseOwner(domain)
    root_exit = closer.root_exit_task
    reap = start_reap(proc, closer)
    original_signal = os.killpg

    def denied(pgid: int, _signum: int) -> None:
        assert pgid == proc.pid
        raise PermissionError(errno.EPERM, "private live group signal refusal")

    monkeypatch.setattr("base.native_process.exec_domain.os.killpg", denied)
    try:
        failures = await asyncio.wait_for(
            settle_resources(root_exit, reap, closer, None, request_stop=True),
            timeout=0.5,
        )
        assert failures[0].stage == "domain_close"
        assert isinstance(failures[0].error, PermissionError)
        assert "private live group signal refusal" in str(failures[0].error)
        assert closer.task.done() and reap.done() and root_exit.cancelled()
        assert proc.returncode is None and native.live()
    finally:
        monkeypatch.setattr("base.native_process.exec_domain.os.killpg", original_signal)
        domain.close_confirmed(time.monotonic() + 5)
        proc.wait(timeout=5)
        root_exit.cancel()
        await asyncio.gather(root_exit, closer.task, reap, return_exceptions=True)


async def _assert_unfinished_service_join(service: HostedServiceResources) -> None:
    with pytest.raises(TimeoutError, match="hosted resource service join"):
        await service.aclose(deadline=asyncio.get_running_loop().time() + 0.05)
    assert not service.joined


def test_cancelled_late_reader_does_not_block_runner_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_path = tmp_path / "detached-helper.pid"
    code = (
        "import pathlib,subprocess,sys; "
        "p=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(60)'],"
        "start_new_session=True); pathlib.Path(sys.argv[1]).write_text(str(p.pid)); "
        "print('private reader fixture',flush=True)"
    )
    spawned: list[tuple[subprocess.Popen[bytes], ExecProcessDomain]] = []
    failures: list[BaseException] = []
    helper: list[OwnedProcess] = []
    main_done, runner_done = threading.Event(), threading.Event()
    service = HostedServiceResources()
    scope = HostedTurnResources(service=service)

    def private_spawn(
        *_args: object, **_kwargs: object
    ) -> tuple[subprocess.Popen[bytes], ExecProcessDomain]:
        owned = ExecProcessDomain.launch_posix(
            [sys.executable, "-I", "-B", "-c", code, str(pid_path)],
            new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        spawned.append(owned)
        return owned

    async def run() -> None:
        await service.turn()
        outcome, _ = await _subprocess._run_legacy_subprocess(
            "private reader fixture",
            exec_context(None, resources=scope),
            asyncio.Event(),
            20,
            exec_dir=tmp_path / "exec",
            accumulation_max_chars=1_000_000,
        )
        assert isinstance(outcome, _ExecCrashed)
        assert isinstance(outcome.exc, ExecTeardownError)
        assert [failure.stage for failure in outcome.exc.failures] == ["reader_join"]
        assert spawned[0][0].returncode == 0
        helper.append(OwnedProcess.capture(psutil.Process(int(pid_path.read_text()))))
        assert helper[0].live()
        assert scope.unresolved and scope.completions
        await _assert_unfinished_service_join(service)
        main_done.set()

    def run_in_thread() -> None:
        try:
            asyncio.run(run())
        except BaseException as exc:
            failures.append(exc)
        finally:
            runner_done.set()

    monkeypatch.setattr(_subprocess, "_spawn", private_spawn)
    runner = threading.Thread(target=run_in_thread, daemon=True)
    runner.start()
    try:
        assert main_done.wait(12), failures
        assert runner_done.wait(0.5), "cancelled late observer blocked Runner shutdown"
        assert not failures
        assert helper[0].live()  # Observation did not acquire detached-child kill authority.
        assert scope.unresolved and all(path.is_file() for path in scope.unresolved)
    finally:
        if not helper and pid_path.exists():
            helper.append(OwnedProcess.capture(psutil.Process(int(pid_path.read_text()))))
        for identity in helper:
            identity.send_signal(signal.SIGKILL)
        for proc, domain in spawned:
            if proc.returncode is None:
                domain.close_confirmed(time.monotonic() + 5)
                proc.wait(timeout=5)
        runner.join(timeout=5)
        assert not runner.is_alive()


async def test_teardown_failure_is_returned_as_crash_with_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    real_settle_resources = _process.settle_resources
    teardown_failure = RuntimeError("synthetic reader teardown failure")

    async def _fail_after_settling(
        *args: Any, **kwargs: Any
    ) -> tuple[_process.TeardownFailure, ...]:
        assert not await real_settle_resources(*args, **kwargs)
        return (_process.TeardownFailure("reader_join", teardown_failure),)

    monkeypatch.setattr(_process, "settle_resources", _fail_after_settling)

    result, _payload = await _subprocess._run_in_subprocess(
        database,
        "print('partial before teardown')",
        exec_context(_AGENT_ID),
        asyncio.Event(),
        30.0,
        None,
        exec_dir=tmp_path / "exec",
        accumulation_max_chars=1_000_000,
    )

    assert isinstance(result, _ExecCrashed)
    assert isinstance(result.exc, _process.ExecTeardownError)
    assert "partial before teardown" in result.output
    assert "reader_join: RuntimeError: synthetic reader teardown failure" in result.output
