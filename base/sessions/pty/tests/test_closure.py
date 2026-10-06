"""Known-target closure is bounded and does not discover arbitrary descendants."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Iterable, Iterator

import psutil
import pytest

from base.native_process.os_platform import IS_WINDOWS
from base.native_process.ownership import OwnedProcess
from base.sessions.pty import closure, process_groups

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="POSIX process groups")


@pytest.fixture
def leaders() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for process in started:
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def _session(leaders: list[subprocess.Popen[bytes]]) -> closure.Target:
    process = subprocess.Popen(["sleep", "300"], start_new_session=True)
    leaders.append(process)
    return closure.Target("ava-test-best-effort", OwnedProcess.capture(psutil.Process(process.pid)))


def test_idle_shell_closes_without_claiming_all_descendants_gone(
    leaders: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _session(leaders)

    def no_scan(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("terminal closure must not enumerate the host")

    with monkeypatch.context() as patch:
        patch.setattr(psutil, "process_iter", no_scan)
        patch.setattr(psutil, "pids", no_scan)
        outcome = closure.close_sessions([target], grace_s=0.2, kill_s=0.2)
    assert outcome == closure.Outcome()
    assert not target.shell.live()


def test_unknown_independent_process_is_left_to_operations(
    leaders: list[subprocess.Popen[bytes]],
) -> None:
    target = _session(leaders)
    bystander = _session(leaders)
    outcome = closure.close_sessions([target], grace_s=0.2, kill_s=0.2)
    assert outcome == closure.Outcome()
    assert bystander.shell.live()


def test_stale_birth_is_never_signalled(leaders: list[subprocess.Popen[bytes]]) -> None:
    target = _session(leaders)
    stale = OwnedProcess(
        target.shell.pid,
        target.shell.birth - 1,
        None if target.shell.starttime is None else target.shell.starttime - 1,
    )
    assert (
        closure.close_sessions([closure.Target(target.name, stale)], grace_s=0, kill_s=0)
        == closure.Outcome()
    )
    assert target.shell.live()


def test_known_shell_survivor_is_reported_not_closed(
    leaders: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _session(leaders)

    def no_signal(_identities: Iterable[OwnedProcess], _signum: int) -> None:
        pass

    monkeypatch.setattr(process_groups, "signal", no_signal)
    outcome = closure.close_sessions([target], grace_s=0, kill_s=0)
    assert outcome.closed == ()
    assert outcome.survivors == (closure.Survivor(target.name, target.shell, "terminal"),)


def test_known_job_survivor_is_diagnostic_after_shell_closes(
    leaders: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    target, job = _session(leaders), _session(leaders)
    real_signal = process_groups.signal

    def signal_shell_only(identities: object, signum: int) -> None:
        del identities
        real_signal([target.shell], signum)

    monkeypatch.setattr(process_groups, "signal", signal_shell_only)
    outcome = closure.close_sessions(
        [closure.Target(target.name, target.shell, (job.shell,))], grace_s=0.2, kill_s=0.2
    )
    assert outcome.survivors == (closure.Survivor(target.name, job.shell, "job"),)
    assert outcome.closed == (
        closure.ClosedSession(target.name, target.shell, ((job.shell.pid, "sleep"),)),
    )


def test_signal_permission_error_is_not_silent_success(
    leaders: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _session(leaders)

    def denied(_group: int, _signum: int) -> None:
        raise PermissionError("known group signal denied")

    with monkeypatch.context() as patch:
        patch.setattr(os, "killpg", denied)
        with pytest.raises(PermissionError, match="known group signal denied"):
            closure.close_sessions([target], grace_s=0, kill_s=0)
    assert target.shell.live()


def test_wire_outcome_round_trips_known_survivors() -> None:
    shell = OwnedProcess(4242, 12.5, 987654)
    job = OwnedProcess(4243, 12.75, None)
    outcome = closure.Outcome(
        (closure.ClosedSession("ava-agent-1-shell-1-wire", shell, ((4243, "sleep"),)),),
        (closure.Survivor("ava-agent-1-shell-1-wire", job, "job"),),
    )
    assert closure.Outcome.from_wire(outcome.to_wire()) == outcome


def test_known_group_force_kill_covers_inheriting_child(
    leaders: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    import select
    import sys

    program = (
        "import os,signal,time; "
        "signal.signal(signal.SIGHUP,signal.SIG_IGN); "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "child=os.fork(); "
        "print(child,flush=True) if child else None; "
        "time.sleep(300)"
    )
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", program], start_new_session=True, stdout=subprocess.PIPE
    )
    leaders.append(process)
    assert process.stdout is not None
    assert select.select([process.stdout], [], [], 10)[0], "job did not become ready"
    child_pid = int(process.stdout.readline())
    child = OwnedProcess.capture(psutil.Process(child_pid))
    shell = OwnedProcess.capture(psutil.Process(process.pid))
    sent: list[int] = []
    real_killpg = os.killpg

    def send(group: int, signum: int) -> None:
        sent.append(signum)
        real_killpg(group, signum)

    monkeypatch.setattr(os, "killpg", send)
    try:
        outcome = closure.close_sessions(
            [closure.Target("known-group", shell)], grace_s=0.05, kill_s=1
        )
        assert outcome == closure.Outcome()
        assert signal.SIGKILL in sent
        assert signal.SIGSTOP not in sent
        assert process_groups.wait([child], 1) == ()
    finally:
        child.send_signal(signal.SIGKILL)
        process.stdout.close()


def test_known_process_in_callers_group_does_not_signal_whole_group() -> None:
    process = subprocess.Popen(["sleep", "300"])
    try:
        identity = OwnedProcess.capture(psutil.Process(process.pid))
        assert os.getpgid(process.pid) == os.getpgrp()
        process_groups.signal([identity], signal.SIGKILL)
        assert process_groups.wait([identity], 1) == ()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
