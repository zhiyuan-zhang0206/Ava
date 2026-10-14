"""The system `ProcessControl` against a real child, and `FakeProcessControl`'s own contract."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Iterator

import pytest

from base.native_process.process_control import SYSTEM_PROCESS_CONTROL
from base.native_process.tests.fake_process_control import FakeProcessControl, Signalled


@pytest.fixture
def child() -> Iterator[subprocess.Popen[bytes]]:
    """A sleeping child that leads a process group of its own."""
    process = subprocess.Popen(["sleep", "60"], process_group=0)
    try:
        yield process
    finally:
        if process.returncode is None:
            process.kill()
        process.wait(timeout=5)


def test_system_reads_the_group_and_session_of_a_real_child(
    child: subprocess.Popen[bytes],
) -> None:
    assert SYSTEM_PROCESS_CONTROL.getpgid(child.pid) == child.pid
    assert SYSTEM_PROCESS_CONTROL.getsid(child.pid) == os.getsid(0)


def test_system_probe_signal_zero_reaches_a_live_process_and_group(
    child: subprocess.Popen[bytes],
) -> None:
    SYSTEM_PROCESS_CONTROL.kill(child.pid, 0)
    SYSTEM_PROCESS_CONTROL.killpg(child.pid, 0)
    assert child.poll() is None


def test_system_group_signal_ends_the_child(child: subprocess.Popen[bytes]) -> None:
    SYSTEM_PROCESS_CONTROL.killpg(child.pid, signal.SIGTERM)
    assert child.wait(timeout=5) == -signal.SIGTERM


def test_system_process_signal_ends_the_child(child: subprocess.Popen[bytes]) -> None:
    SYSTEM_PROCESS_CONTROL.kill(child.pid, signal.SIGKILL)
    assert child.wait(timeout=5) == -signal.SIGKILL


def test_system_signal_to_a_reaped_child_raises_what_os_raises(
    child: subprocess.Popen[bytes],
) -> None:
    child.kill()
    child.wait(timeout=5)
    with pytest.raises(ProcessLookupError):
        SYSTEM_PROCESS_CONTROL.getpgid(child.pid)


def test_fake_records_signals_in_order_and_sends_none() -> None:
    control = FakeProcessControl()
    control.killpg(4242, signal.SIGTERM)
    control.kill(77, signal.SIGKILL)
    control.killpg(4242, 0)
    assert control.signals == [
        Signalled("killpg", 4242, signal.SIGTERM),
        Signalled("kill", 77, signal.SIGKILL),
        Signalled("killpg", 4242, 0),
    ]


def test_fake_refusal_raises_after_recording_the_attempt() -> None:
    control = FakeProcessControl()
    control.refuse(4242, PermissionError("operation not permitted"))
    with pytest.raises(PermissionError, match="operation not permitted"):
        control.killpg(4242, signal.SIGKILL)
    control.killpg(4343, signal.SIGKILL)
    assert control.signals == [
        Signalled("killpg", 4242, signal.SIGKILL),
        Signalled("killpg", 4343, signal.SIGKILL),
    ]


def test_fake_answers_groups_and_sessions_from_its_table_and_refuses_the_rest() -> None:
    control = FakeProcessControl(groups={10: 10, 11: 10}, sessions={10: 5})
    assert (control.getpgid(10), control.getpgid(11), control.getsid(10)) == (10, 10, 5)
    with pytest.raises(ProcessLookupError):
        control.getpgid(12)
    with pytest.raises(ProcessLookupError):
        control.getsid(11)


def test_fake_on_signal_interleaves_with_the_tests_other_events() -> None:
    events: list[str] = []
    control = FakeProcessControl(on_signal=lambda sent: events.append(f"signal {sent.target}"))
    events.append("listing")
    control.killpg(9, signal.SIGKILL)
    events.append("sample")
    assert events == ["listing", "signal 9", "sample"]


def test_fake_forwards_to_the_system_after_recording(child: subprocess.Popen[bytes]) -> None:
    control = FakeProcessControl(forward_to=SYSTEM_PROCESS_CONTROL)
    assert control.getpgid(child.pid) == child.pid
    control.killpg(child.pid, signal.SIGKILL)
    assert child.wait(timeout=5) == -signal.SIGKILL
    assert control.signals == [Signalled("killpg", child.pid, signal.SIGKILL)]


def test_fake_refusal_stops_the_forwarded_signal(child: subprocess.Popen[bytes]) -> None:
    control = FakeProcessControl(forward_to=SYSTEM_PROCESS_CONTROL)
    control.refuse(child.pid, PermissionError("denied"))
    with pytest.raises(PermissionError):
        control.killpg(child.pid, signal.SIGKILL)
    assert child.poll() is None
