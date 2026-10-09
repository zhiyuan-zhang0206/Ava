"""`close_all`: the one terminal closure, run by the service that holds the masters.

Real shells, real signals. The shells get SIGHUP and a known foreground group
gets SIGTERM; known targets alive when the grace ends receive SIGKILL
(docs/decisions/runtime/processes/shutdown/2026-09-28-stop-escalates-to-sigkill.md). A busy session whose shell
the closure verified gone comes back in `Outcome.closed`, which is what a stop
turns into owner notices.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import psutil
import pytest

from base.native_process.os_platform import is_windows
from base.sessions.pty import client, closure
from base.sessions.pty.tests.job_wait import wait_for_foreground, wait_for_job
from tests.path_scoped import pty_jobs as jobs
from tests.path_scoped import pty_shells as support
from tests.path_scoped.pty_service import pty_service as pty_service
from tests.path_scoped.pty_shells import new, output_until, type_line, wait_for

pytestmark = [
    pytest.mark.skipif(is_windows(), reason="pty sessions are POSIX-only"),
    pytest.mark.usefixtures("pty_service"),
]


def _names(outcome: closure.Outcome) -> list[str]:
    return [closed.name for closed in outcome.closed]


def test_a_regular_job_and_its_shell_close_within_the_grace(unit_home: Path) -> None:
    name = "ava-agent-987-shell-2045-job"
    shell = jobs.start(name, unit_home, jobs.TERM_OK)
    output_until(name, "job-ready")
    (job,) = jobs.live_children(shell)
    wait_for_foreground(job)
    (info,) = client.list_sessions()

    started = time.monotonic()
    outcome = client.close_all(grace_s=10.0, kill_s=3.0)

    assert time.monotonic() - started < 8, "a job that handles TERM must not cost the whole grace"
    assert _names(outcome) == [name]
    assert outcome.closed[0].shell.pid == info.pid
    assert outcome.closed[0].left == ()
    assert outcome.survivors == ()
    assert jobs.wait_exit(job.pid), "the job must be closed"
    assert client.list_sessions() == []


def test_an_idle_session_closes_silently(unit_home: Path) -> None:
    """Idle is a shell with nothing beyond it: it is closed, and it is not busy, so the
    outcome names nothing to tell its owner (issue #2044 #3)."""
    (unit_home / ".bash_profile").write_text("PS1='ava-idle-shell> '\n")
    name = "ava-agent-987-shell-2044-idle"
    new(name, unit_home, {"HOME": str(unit_home)})
    assert wait_for(lambda: support.screen(name).rstrip().endswith("ava-idle-shell>"))
    shell = support.shell_process(name)

    outcome = client.close_all(grace_s=5.0, kill_s=3.0)

    assert outcome.closed == ()
    assert jobs.wait_exit(shell.pid)
    assert client.list_sessions() == []


def test_a_restart_loop_shell_cannot_outlive_the_closure(unit_home: Path) -> None:
    """HUP goes to the shell first, then the known foreground job gets TERM."""
    name = "ava-agent-987-shell-2045-loop"
    new(name, unit_home, cmd=jobs.LOOP_SHELL)
    shell = support.shell_process(name)
    wait_for_foreground(wait_for_job(shell, ["bash", "-c", "while true; do sleep 1; done"]))

    outcome = client.close_all(grace_s=10.0, kill_s=3.0)

    assert _names(outcome) == [name]
    assert jobs.wait_exit(shell.pid), "the restart loop shell must exit"


def test_a_job_that_ignores_termination_is_killed_after_its_grace(unit_home: Path) -> None:
    name = "ava-agent-987-shell-2045-stubborn"
    shell = jobs.start(name, unit_home, jobs.STUBBORN)
    output_until(name, "stubborn-ready")
    (job,) = jobs.live_children(shell)
    wait_for_foreground(job)

    started = time.monotonic()
    outcome = client.close_all(grace_s=1.0, kill_s=3.0)

    assert time.monotonic() - started < 8, "the grace bounds the wait"
    assert jobs.wait_exit(job.pid, timeout=5), "the job outlived the closure"
    assert _names(outcome) == [name], "a killed busy session still comes back for its notice"
    assert outcome.survivors == ()


@pytest.mark.parametrize("background_exited", [False, True])
def test_an_untracked_nohup_job_does_not_block_terminal_closure(
    unit_home: Path, *, background_exited: bool
) -> None:
    """Close the known terminal whether an untracked background job lives or exits."""
    name = "ava-agent-987-shell-2049-nohup"
    new(name, unit_home)
    shell = support.shell_process(name)
    marker = unit_home / "nohup.pid"
    type_line(name, f"nohup sleep 300 > /dev/null 2>&1 & echo $! > {marker}")
    member = jobs.wait_for_file(marker, "the nohup job")
    try:
        assert wait_for_job(shell, ["sleep", "300"]).pid == member
        wait_for_foreground(shell)
        if background_exited:
            jobs.kill_quietly(member)
            assert wait_for(lambda: not psutil.pid_exists(member)), "the test job must exit"
        outcome = client.close_all(grace_s=1.0, kill_s=3.0)
        assert jobs.wait_exit(shell.pid), "the known shell must close"
        assert client.list_sessions() == [], "the master must close despite a leftover job"
        # The shell/OS may also end an untracked job; its survival is not promised.
        assert outcome.survivors == (), "untracked jobs are not certified or reported"
    finally:
        jobs.kill_quietly(member)


def test_an_escaped_child_holding_the_slave_does_not_block_master_teardown(unit_home: Path) -> None:
    name = "ava-agent-987-shell-escaped-slave"
    marker = unit_home / "escaped.pid"
    shell = jobs.create(name, unit_home, jobs.setsid_child(marker, escape=True))
    child = jobs.wait_for_file(marker, "the escaped worker")
    assert wait_for(lambda: psutil.Process(child).ppid() != shell.pid)
    assert wait_for(lambda: support.screen(name).rstrip().endswith(("$", "#")))
    try:
        outcome = client.close_all(grace_s=0.5, kill_s=3.0)
        assert jobs.wait_exit(shell.pid)
        assert client.list_sessions() == [], "master teardown must not wait for slave EOF"
        assert psutil.pid_exists(child), "the escaped worker is not a known target"
        assert outcome.survivors == ()
    finally:
        jobs.kill_quietly(child)


def test_the_closure_covers_every_session_at_once(unit_home: Path) -> None:
    busy = [f"ava-agent-987-shell-{n}-multi" for n in range(5)]
    shells = [jobs.start(name, unit_home, jobs.STUBBORN) for name in busy]
    for name, shell in zip(busy, shells, strict=True):
        output_until(name, "stubborn-ready")
        (job,) = jobs.live_children(shell)
        wait_for_foreground(job)
    idle = "ava-agent-987-shell-99-quiet"
    new(idle, unit_home)

    outcome = client.close_all(grace_s=1.0, kill_s=3.0)

    assert sorted(_names(outcome)) == sorted(busy)
    assert all(jobs.wait_exit(shell.pid) for shell in shells)
    assert client.list_sessions() == []


def test_a_closure_over_no_sessions_is_empty() -> None:
    assert client.close_all(grace_s=1.0, kill_s=1.0) == closure.Outcome()


def test_no_session_is_born_while_the_closure_runs(unit_home: Path) -> None:
    """A stop closes the terminals after the agents drained; an allocation that slips in
    meanwhile would outlive the closure. Allocation is refused for its duration and open
    again once it is done."""
    jobs.start("ava-agent-987-shell-2060-slow", unit_home, jobs.STUBBORN)
    output_until("ava-agent-987-shell-2060-slow", "stubborn-ready")
    closing = threading.Thread(target=client.close_all, kwargs={"grace_s": 3.0, "kill_s": 3.0})
    closing.start()
    try:
        refusal: list[client.ServiceError] = []

        def refused() -> bool:
            try:
                new("ava-agent-987-shell-2061-late", unit_home)
            except client.ServiceError as exc:
                refusal.append(exc)
                return True
            client.kill("ava-agent-987-shell-2061-late", graceful=False)
            return False

        assert wait_for(refused, timeout=5, interval=0.05)
        assert "closing" in str(refusal[0])
    finally:
        closing.join(timeout=60)
    assert new("ava-agent-987-shell-2061-late", unit_home) is True


def test_the_closure_outcome_survives_the_wire(unit_home: Path) -> None:
    name = "ava-agent-987-shell-2070-wire"
    jobs.start(name, unit_home, jobs.STUBBORN)
    output_until(name, "stubborn-ready")
    (info,) = client.list_sessions()

    outcome = client.close_all(grace_s=0.5, kill_s=3.0)

    (closed,) = outcome.closed
    assert (closed.shell.pid, closed.shell.birth) == (info.pid, info.create_time)
    assert closed.shell.starttime == info.starttime
    assert closure.Outcome.from_wire(outcome.to_wire()) == outcome
