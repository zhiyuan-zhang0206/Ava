"""The terminal closure on plain session leaders, with the SIGKILL leg's refusals simulated.

A `/bin/sh` started in its own session stands in for a login shell: it leads a POSIX
session and holds a job. The real-pty behavior of the closure is covered end to end by
the service tests (`services/pty_sessions/tests/test_close_all.py`); this file pins what
only a simulated refusal can: a process this user may not signal (another user's, such as
a root `sudo` on the pty) that outlives the SIGKILL.
"""

from __future__ import annotations

import contextlib
import subprocess
import time
from collections.abc import Iterable, Iterator
from typing import Any

import psutil
import pytest

from base.native_process.os_platform import IS_WINDOWS
from base.native_process.ownership import OwnedProcess
from base.sessions.pty import closure, session_tree

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="POSIX sessions")


@pytest.fixture
def leaders() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for leader in started:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            session_tree_kill(leader.pid)
        leader.wait(timeout=10)


def session_tree_kill(pid: int) -> None:
    for child in psutil.Process(pid).children(recursive=True):
        with contextlib.suppress(psutil.NoSuchProcess):
            child.kill()
    psutil.Process(pid).kill()


def _session(leaders: list[subprocess.Popen[bytes]], *, busy: bool) -> closure.Target:
    # A busy shell ignores the hangup and the job inherits both ignored dispositions, so
    # only the SIGKILL leg (here simulated) can end them.
    script = "trap '' HUP TERM; sleep 300 & wait" if busy else "sleep 300"
    leader = subprocess.Popen(  # noqa: S603 — a bystander session leader
        ["/bin/sh", "-c", script], start_new_session=True
    )
    leaders.append(leader)
    shell = OwnedProcess.capture(psutil.Process(leader.pid))
    if busy:
        deadline = time.monotonic() + 10
        while not psutil.Process(leader.pid).children():
            assert time.monotonic() < deadline, "the job never started"
            time.sleep(0.02)
    return closure.Target(f"ava-agent-1-shell-{len(leaders)}-sim", shell)


def _denied_but_the_shell(leader: OwnedProcess, **kwargs: Any) -> session_tree.TreeKill:
    """A session kill that ends the shell but may not signal the rest of its session."""
    with contextlib.suppress(psutil.NoSuchProcess):
        psutil.Process(leader.pid).kill()
    deadline = time.monotonic() + 10
    while leader.live() and time.monotonic() < deadline:
        time.sleep(0.02)
    left = tuple(identity for identity in kwargs["also"] if identity != leader)
    return session_tree.TreeKill((leader,), left, left)


def _denied(
    leader: OwnedProcess, *, also: Iterable[OwnedProcess] = (), **_kwargs: Any
) -> session_tree.TreeKill:
    del leader
    captured = tuple(also)
    return session_tree.TreeKill((), captured, captured)


def test_a_process_the_closure_may_not_signal_is_reported_with_its_closed_session(
    leaders: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kill ends the shell, but a job outlives its SIGKILL. The session is over for its
    owner, so it is `closed`, naming the process left running; the process is a `Survivor`."""
    target = _session(leaders, busy=True)
    (job,) = psutil.Process(target.shell.pid).children()
    monkeypatch.setattr(session_tree, "kill_session_tree", _denied_but_the_shell)

    outcome = closure.close_sessions([target], grace_s=0.1, kill_s=0.5)

    (closed,) = outcome.closed
    assert closed.name == target.name
    assert closed.left == ((job.pid, "sleep"),)
    (survivor,) = outcome.survivors
    assert (survivor.session, survivor.process.pid, survivor.role) == (target.name, job.pid, "job")
    assert closure.Outcome.from_wire(outcome.to_wire()) == outcome


def test_a_session_whose_shell_lives_is_not_closed_and_its_shell_is_the_survivor(
    leaders: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry sees such a session again, so the closure must not claim it: no notice
    for a session an owner can still use."""
    target = _session(leaders, busy=True)
    monkeypatch.setattr(session_tree, "kill_session_tree", _denied)

    outcome = closure.close_sessions([target], grace_s=0.1, kill_s=0.5)

    assert outcome.closed == ()
    assert target.shell.pid in {s.process.pid for s in outcome.survivors if s.role == "terminal"}


def test_one_stuck_session_does_not_hide_the_session_that_closed(
    leaders: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each closed busy session is reported even while another keeps the closure incomplete."""
    closing, stuck = _session(leaders, busy=True), _session(leaders, busy=True)
    real = session_tree.kill_session_tree

    def kill(leader: OwnedProcess, **kwargs: Any) -> session_tree.TreeKill:
        return _denied(leader, **kwargs) if leader == stuck.shell else real(leader, **kwargs)

    monkeypatch.setattr(session_tree, "kill_session_tree", kill)

    outcome = closure.close_sessions([closing, stuck], grace_s=0.1, kill_s=2.0)

    assert [c.name for c in outcome.closed] == [closing.name]
    assert {s.session for s in outcome.survivors} == {stuck.name}


def test_an_idle_session_is_closed_without_being_reported(
    leaders: list[subprocess.Popen[bytes]],
) -> None:
    target = _session(leaders, busy=False)

    outcome = closure.close_sessions([target], grace_s=3.0, kill_s=2.0)

    assert outcome == closure.Outcome()
    assert not target.shell.live()


def test_a_target_that_is_no_longer_its_recorded_process_is_skipped(
    leaders: list[subprocess.Popen[bytes]],
) -> None:
    target = _session(leaders, busy=True)
    recycled = closure.Target(
        target.name,
        OwnedProcess(
            target.shell.pid,
            target.shell.birth - 500.0,
            target.shell.starttime and target.shell.starttime - 1,
        ),
    )

    outcome = closure.close_sessions([recycled], grace_s=0.1, kill_s=0.5)

    assert outcome == closure.Outcome()
    assert target.shell.live(), "the process now at the recorded pid was signalled"


def test_the_wire_form_round_trips_an_outcome_with_a_linux_start_tick() -> None:
    shell = OwnedProcess(4242, 12.5, 987654)
    job = OwnedProcess(4243, 12.75, None)
    outcome = closure.Outcome(
        (closure.ClosedSession("ava-agent-1-shell-1-wire", shell, ((4243, "sleep"),)),),
        (closure.Survivor("ava-agent-1-shell-1-wire", job, "job"),),
    )
    assert closure.Outcome.from_wire(outcome.to_wire()) == outcome
