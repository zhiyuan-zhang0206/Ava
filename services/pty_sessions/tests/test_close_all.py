"""`close_all`: the one terminal closure, run by the service that holds the masters.

Real shells, real signals. The shells get SIGHUP and every other member of their
POSIX session SIGTERM; what is alive when the grace ends is SIGKILLed whole
(decisions/2026-09-28-stop-escalates-to-sigkill.md). A busy session whose shell
the closure verified gone comes back in `Outcome.closed`, which is what a stop
turns into owner notices.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import psutil
import pytest

from base.native_process.os_platform import IS_WINDOWS
from base.sessions.pty import client, closure
from services.pty_sessions.tests import jobs, support
from services.pty_sessions.tests.support import new, type_line, wait_for
from tests.path_scoped.pty_service import pty_service as pty_service

pytestmark = [
    pytest.mark.skipif(IS_WINDOWS, reason="pty sessions are POSIX-only"),
    pytest.mark.usefixtures("pty_service"),
]


def _names(outcome: closure.Outcome) -> list[str]:
    return [closed.name for closed in outcome.closed]


def test_a_regular_job_and_its_shell_close_within_the_grace(unit_home: Path) -> None:
    name = "ava-agent-987-shell-2045-job"
    shell = jobs.start(name, unit_home, jobs.TERM_OK)
    (job,) = jobs.live_children(shell)
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
    """HUP goes to the shell first (stopping the spawner), then the descendants get
    their TERM: the ordering proven in the field (#2045)."""
    name = "ava-agent-987-shell-2045-loop"
    new(name, unit_home, cmd=jobs.LOOP_SHELL)
    shell = support.shell_process(name)
    assert wait_for(lambda: bool(jobs.live_children(shell)))

    outcome = client.close_all(grace_s=10.0, kill_s=3.0)

    assert _names(outcome) == [name]
    assert jobs.wait_exit(shell.pid), "the restart loop shell must exit"


def test_a_job_that_ignores_termination_is_killed_after_its_grace(unit_home: Path) -> None:
    name = "ava-agent-987-shell-2045-stubborn"
    shell = jobs.start(name, unit_home, jobs.STUBBORN)
    (job,) = jobs.live_children(shell)

    started = time.monotonic()
    outcome = client.close_all(grace_s=1.0, kill_s=3.0)

    assert time.monotonic() - started < 8, "the grace bounds the wait"
    assert jobs.wait_exit(job.pid, timeout=5), "the job outlived the closure"
    assert _names(outcome) == [name], "a killed busy session still comes back for its notice"
    assert outcome.survivors == ()


def test_a_double_forked_orphan_of_the_session_is_killed(unit_home: Path) -> None:
    """A process that double-forked out of the shell's tree stays in its POSIX session:
    the closure captures it before the hangup, and it dies with the rest."""
    name = "ava-agent-987-shell-2046-orphan"
    pidfile = unit_home / "orphan.pid"
    shell = jobs.start(name, unit_home, jobs.double_forked(pidfile, ignore=("SIGTERM", "SIGHUP")))
    orphan = jobs.wait_for_file(pidfile, "the double-forked worker")
    assert wait_for(lambda: psutil.Process(orphan) not in jobs.live_children(shell))
    assert psutil.Process(orphan).ppid() != shell.pid, "precondition: it left the shell's tree"

    try:
        outcome = client.close_all(grace_s=1.0, kill_s=3.0)
        assert jobs.wait_exit(orphan, timeout=5), "the orphan outlived a closure that succeeded"
        assert _names(outcome) == [name]
    finally:
        jobs.kill_quietly(orphan)


def test_a_hangup_ignoring_worker_outside_the_shell_tree_is_terminated(unit_home: Path) -> None:
    """The cancel reaches the whole session: TERM finds a hangup-ignoring worker that
    left the shell's tree through the shell's POSIX session."""
    name = "ava-agent-987-shell-2045-orphan"
    pidfile = unit_home / "orphan.pid"
    jobs.create(name, unit_home, jobs.double_forked(pidfile, ignore=("SIGHUP",)))
    orphan = jobs.wait_for_file(pidfile, "the double-forked worker")
    try:
        client.close_all(grace_s=15.0, kill_s=3.0)
        assert jobs.wait_exit(orphan, timeout=2), "the closure left the session's worker running"
    finally:
        jobs.kill_quietly(orphan)


def test_a_nohup_member_that_ignores_the_hangup_is_killed(unit_home: Path) -> None:
    """`nohup cmd &` from the interactive shell survives the shell's own hangup, so
    only the SIGKILL leg ends it."""
    name = "ava-agent-987-shell-2049-nohup"
    new(name, unit_home)
    marker = unit_home / "nohup.pid"
    type_line(name, f"nohup sleep 300 > /dev/null 2>&1 & echo $! > {marker}")
    member = jobs.wait_for_file(marker, "the nohup job")
    assert psutil.pid_exists(member)
    try:
        outcome = client.close_all(grace_s=1.0, kill_s=3.0)
        assert jobs.wait_exit(member, timeout=5), "the nohup member outlived the closure"
        assert _names(outcome) == [name]
    finally:
        jobs.kill_quietly(member)


def test_a_setsid_member_still_inside_the_tree_is_killed(unit_home: Path) -> None:
    """A worker that called setsid(2) but is still a descendant of the shell is covered
    by the descendant walk, whatever it ignores."""
    name = "ava-agent-987-shell-2050-setsid"
    pidfile = unit_home / "setsid.pid"
    shell = jobs.create(name, unit_home, jobs.setsid_child(pidfile, escape=False))
    worker = jobs.wait_for_file(pidfile, "the setsid worker")
    assert psutil.Process(worker).ppid() == psutil.Process(shell.pid).children()[0].pid
    assert psutil.Process(worker) in shell.children(recursive=True)
    try:
        outcome = client.close_all(grace_s=1.0, kill_s=3.0)
        assert jobs.wait_exit(worker, timeout=5), "the setsid member outlived the closure"
        assert _names(outcome) == [name]
    finally:
        jobs.kill_quietly(worker)


def test_a_setsid_worker_that_left_the_tree_is_sovereign(unit_home: Path) -> None:
    """The boundary: a process that called setsid AND left the shell's tree has left the
    session by the kernel's own definition. That is how Ava launches a sovereign process
    from inside a shell, so the closure leaves it running (session_tree's docstring)."""
    name = "ava-agent-987-shell-2051-sovereign"
    pidfile = unit_home / "sovereign.pid"
    new(name, unit_home, cmd=f"python3 -u {unit_home / 'sovereign.job.py'}")
    # The program is written after the session exists: the initial command runs it
    # once the shell is ready, which is after this write.
    (unit_home / "sovereign.job.py").write_text(
        jobs.setsid_child(pidfile, escape=True), encoding="utf-8"
    )
    worker = jobs.wait_for_file(pidfile, "the sovereign worker")
    shell = support.shell_process(name)
    # The job exits half a second after forking the worker; only then has the worker left the tree.
    assert wait_for(lambda: psutil.Process(worker) not in shell.children(recursive=True))
    try:
        client.close_all(grace_s=1.0, kill_s=3.0)
        assert psutil.pid_exists(worker), "a sovereign process must survive the closure"
    finally:
        jobs.kill_quietly(worker)


@pytest.mark.parametrize("disposition", ["SIG_IGN", "SIG_DFL"])
def test_a_helper_the_job_forks_on_term_and_orphans_is_killed(
    disposition: str, unit_home: Path
) -> None:
    """The job's TERM handler forks a helper and exits at once: no captured process is
    left alive to lead the closure to the helper (with SIG_DFL it never even receives a
    signal), but it is still in the shell's POSIX session. The closure takes it."""
    name = "ava-agent-987-shell-2047-helper"
    pidfile = unit_home / "helper.pid"
    source = jobs.FORK_ON_TERM.format(disposition=disposition)
    jobs.start(name, unit_home, source, args=str(pidfile))
    assert support.wait_for(Path(f"{pidfile}.ready").exists, 15), "the TERM handler never installed"

    outcome = client.close_all(grace_s=1.0, kill_s=3.0)

    helper = jobs.wait_for_file(pidfile, "the helper")
    try:
        assert jobs.wait_exit(helper, timeout=5), "the helper outlived a closure that succeeded"
        assert _names(outcome) == [name]
    finally:
        jobs.kill_quietly(helper)


@pytest.mark.parametrize(
    ("hop_ms", "hops", "grace_s"), [(3, 60, 2.0), (8, 40, 2.0), (10, 150, 2.0), (10, 150, 0.5)]
)
def test_the_last_hop_of_a_fork_chain_started_on_term_is_killed(
    hop_ms: int, hops: int, grace_s: float, unit_home: Path
) -> None:
    """A TERM handler starts a chain of processes that each fork the next and exit within
    a few ms, too short for a full process-table pass to read one alive. While the session
    still holds a process the grace keeps polling, a proven pass that reads a hop keeps
    the proof current, and no part of the chain outlives the closure."""
    name = "ava-agent-987-shell-2048-chain"
    pidfile = unit_home / "last.pid"
    source = jobs.FORK_CHAIN.format(hop_ms=hop_ms, hops=hops)
    jobs.start(name, unit_home, source, args=str(pidfile))
    assert support.wait_for(Path(f"{pidfile}.ready").exists, 15), "the TERM handler never installed"
    script = unit_home / f"{name}.job.py"

    outcome = client.close_all(grace_s=grace_s, kill_s=3.0)

    # Wait out the chain's own run: a chain that escaped always has a live hop (each hop
    # forks the next before it exits), or has reached its last one.
    time.sleep(hops * hop_ms / 1000 + 1.0)
    left = [
        process.pid
        for process in psutil.process_iter(["cmdline"])
        if str(script) in (process.info["cmdline"] or ()) and not support.gone(process)
    ]
    try:
        assert not left, f"part of the chain outlived a closure that succeeded: {left}"
        assert _names(outcome) == [name]
    finally:
        for pid in left:
            jobs.kill_quietly(pid)


def test_the_closure_covers_every_session_at_once(unit_home: Path) -> None:
    busy = [f"ava-agent-987-shell-{n}-multi" for n in range(5)]
    shells = [jobs.start(name, unit_home, jobs.STUBBORN) for name in busy]
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
    (info,) = client.list_sessions()

    outcome = client.close_all(grace_s=0.5, kill_s=3.0)

    (closed,) = outcome.closed
    assert (closed.shell.pid, closed.shell.birth) == (info.pid, info.create_time)
    assert closed.shell.starttime == info.starttime
    assert closure.Outcome.from_wire(outcome.to_wire()) == outcome
