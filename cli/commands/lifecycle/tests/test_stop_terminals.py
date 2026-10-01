"""Terminal closure at a normal stop (#2045): real PTYs, real signals.

The 2026-09-09 migration stop timed out on machines whose services had all
exited: the survivors were persistent-shell jobs (watcher / tee) that ignored
SIGTERM. Root cause: the PTY host ignores SIGHUP/SIGTERM/SIGPIPE, and ignored
dispositions survive exec — the interactive bash inherited SIG_IGN and kept it
ignored for itself and every job, so the stop path's per-PID SIGTERM was a
no-op. These tests lock the two parts of the fix:

- the pty child resets the dispositions before exec (TERM/HUP reach the shell
  and its jobs again),
- `close_terminals` HUPs the shells first (stopping restart loops from
  spawning new jobs), then TERMs the rest of each session's captured
  membership.

A captured job is any member of the shell's session (`session_tree`): a worker
that double-forked out of the shell's tree is cancelled like a direct child.
What outlives a bounded grace is SIGKILLed with its whole session
(decisions/2026-09-28-stop-escalates-to-sigkill.md); only a process that
outlives its SIGKILL leaves the maintenance hold in place with a per-phase
diagnostic.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from base.deploy.maintenance import admission
from base.sessions.backend import PtySessionBackend
from base.sessions.pty import session_tree
from cli.commands.lifecycle import _temporary_stop as command
from cli.commands.lifecycle import service_stop as strict
from cli.commands.lifecycle import stop as entry
from cli.commands.lifecycle._maintenance_stop_report import StopIncompleteError
from cli.commands.lifecycle.service_stop import OwnedProcess
from ops import pty_close_notices
from tests.cli.test_pause_stop import dependencies
from tests.cli.test_pause_stop import home as home
from tests.path_scoped.pty_reaper import PtyReaper

# A regular interruptible job: NO signal handlers of its own — it relies on
# the default TERM disposition. Before the fix it inherited SIG_IGN through
# bash (ignored dispositions survive exec), so TERM never reached it.
_TERM_OK_JOB = "import time\nprint('job-ready', flush=True)\nwhile True: time.sleep(0.1)\n"

# A job that ignores the closure's HUP and TERM, so only a SIGKILL ends it.
# Teardown SIGKILLs it (`PtyReaper`), but a test process that is itself killed
# (a tool's timeout, a lost xdist worker) runs no teardown: the job then ends
# once the test process that started it is gone, instead of living on as an
# orphan of init. The pid is this module's importer, the test process itself.
_STUBBORN_JOB = (
    "import os,signal,time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "print('stubborn-ready', flush=True)\n"
    f"while True: time.sleep(0.1); os.kill({os.getpid()}, 0)\n"
)

_LOOP_SHELL = "bash -c 'while true; do sleep 1; done'"

# The idle test's own prompt, set by its home's `.bash_profile`.
_IDLE_PROMPT = "ava-idle-shell>"


def _double_forked_job(pidfile: Path, *, ignore: tuple[str, ...]) -> str:
    """A job whose worker double-forks out of the shell's tree: reparented to
    init, the worker stays in the shell's POSIX session and ignores `ignore`."""
    ignored = "".join(f"        signal.signal(signal.{sig}, signal.SIG_IGN)\n" for sig in ignore)
    staged = f"{pidfile}.tmp"
    return (
        "import os,signal,time\n"
        "if os.fork() == 0:\n"
        "    if os.fork() == 0:\n"
        f"{ignored}"
        f"        open({staged!r}, 'w').write(str(os.getpid()))\n"
        f"        os.rename({staged!r}, {str(pidfile)!r})\n"
        "        while True: time.sleep(0.1)\n"
        "    os._exit(0)\n"
        "os.wait()\n"
    )


# A foreground job whose TERM handler forks a helper and exits at once. The
# helper is born after the stop's signals and its parent is gone before the next
# poll; the shell has already died of its hangup. It keeps the shell's POSIX
# session. `{disposition}` is the helper's own HUP/TERM disposition. It records
# its pid first: with SIG_DFL, a shell slow to handle its own hangup (a loaded
# box) forwards HUP to the job's group, which then includes the helper.
_FORK_ON_TERM_JOB = (
    "import os,signal,sys,time\n"
    "def on_term(*_):\n"
    "    if os.fork() == 0:\n"
    "        open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
    "        os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "        signal.signal(signal.SIGTERM, signal.{disposition})\n"
    "        signal.signal(signal.SIGHUP, signal.{disposition})\n"
    "        while True: time.sleep(0.1)\n"
    "    os._exit(0)\n"
    "signal.signal(signal.SIGTERM, on_term)\n"
    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "open(sys.argv[1] + '.ready', 'w').close()\n"
    "while True: time.sleep(0.1)\n"
)


# A job whose TERM handler starts a fork chain: each hop lives `{hop_ms}` ms,
# forks the next and exits; the last of `{hops}` hops stays. Each hop is gone
# long before a full process-table pass reaches it.
_FORK_CHAIN_JOB = (
    "import os,signal,sys,time\n"
    "def on_term(*_):\n"
    "    if os.fork() == 0:\n"
    "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "        signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "        for _ in range({hops}):\n"
    "            time.sleep({hop_ms} / 1000.0)\n"
    "            if os.fork() != 0:\n"
    "                os._exit(0)\n"
    "        open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
    "        os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "        while True: time.sleep(0.1)\n"
    "    os._exit(0)\n"
    "signal.signal(signal.SIGTERM, on_term)\n"
    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "open(sys.argv[1] + '.ready', 'w').close()\n"
    "while True: time.sleep(0.1)\n"
)


def _has_exited(process: psutil.Process) -> bool:
    """True once the process is a zombie or no longer exists.

    The status read is racy on its own: the process can be reaped between its
    construction and the read, and psutil reports that as NoSuchProcess (task
    #4397 — it evicted #3142 from the merge queue). A vanished process is an
    exited one.
    """
    try:
        return process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _wait_exit(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return True
        if _has_exited(process):
            return True
        time.sleep(0.05)
    return False


def _shell_children(shell: OwnedProcess) -> list[psutil.Process]:
    with contextlib.suppress(psutil.NoSuchProcess, psutil.ZombieProcess):
        return psutil.Process(shell.pid).children(recursive=True)
    return []


def _stop_env(monkeypatch: pytest.MonkeyPatch, home: Path, terminal: PtySessionBackend) -> None:
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(strict, "get_shell_backend", lambda: terminal)
    for name in ("stop_permissions_helper",):
        monkeypatch.setattr(f"cli.commands.lifecycle._stop_extras.{name}", lambda **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(entry, "_announce_stopping", lambda: None)


def _start_busy_session(
    terminal: PtySessionBackend, home: Path, name: str, job: str, reaper: PtyReaper
) -> OwnedProcess:
    # The job lives in a script file: shell quoting of an inline -c program is
    # the flakiest part of the fixture, and the production jobs (watchers) are
    # file-backed the same way.
    script = home / f"{name}.job.py"
    script.write_text(job, encoding="utf-8")
    assert terminal.new_session(name, f"python3 -u {script}", home, env={"AVA_HOME": str(home)})
    return reaper.track_session(name)


def _session_orphan(pidfile: Path, shell: OwnedProcess, reaper: PtyReaper) -> psutil.Process:
    """Pin the double-forked worker once it has left the shell's tree."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not pidfile.exists():
        time.sleep(0.05)
    orphan = psutil.Process(int(pidfile.read_text(encoding="utf-8")))
    reaper.track(orphan)
    while time.monotonic() < deadline and orphan in _shell_children(shell):
        time.sleep(0.05)
    assert orphan not in _shell_children(shell), "precondition: it left the shell's tree"
    assert os.getsid(orphan.pid) == shell.pid, "precondition: it is in the shell's session"
    return orphan


def _started_jobs(shell: OwnedProcess, reaper: PtyReaper) -> list[psutil.Process]:
    """Wait for the shell's first live descendants and pin them for teardown:
    a job that ignores HUP outlives the shell a stop hangs up."""
    jobs: list[psutil.Process] = []
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not jobs:
        jobs = [child for child in _shell_children(shell) if not _has_exited(child)]
        time.sleep(0.1)
    reaper.track(*jobs)
    return jobs


def _unkillable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand-in for processes the closure may not signal (another user's, such
    as a root `sudo` on the pty): the session kill signals nothing and reports
    every captured member it was given as a survivor it was denied."""

    def denied(
        leader: OwnedProcess,
        *,
        also: Iterable[OwnedProcess] = (),
        wait_s: float,
        proven_at: float | None = None,
    ) -> session_tree.TreeKill:
        del leader, wait_s, proven_at
        captured = tuple(also)
        return session_tree.TreeKill((), captured, captured)

    monkeypatch.setattr(session_tree, "kill_session_tree", denied)


def _denied_but_the_shell(leader: OwnedProcess, **kwargs: Any) -> session_tree.TreeKill:
    """Stand-in for a session kill that ends the shell but may not signal the
    rest of its session (another user's processes): every other captured
    member survives it, denied."""
    with contextlib.suppress(psutil.NoSuchProcess):
        psutil.Process(leader.pid).kill()
    assert _wait_exit(leader.pid), "the shell survived its SIGKILL"
    left = tuple(identity for identity in kwargs["also"] if identity != leader)
    return session_tree.TreeKill((leader,), left, left)


@pytest.mark.flaky
def test_fork_shell_child_resets_term_and_hup_dispositions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disposition fix itself: the exec'd login shell sees default TERM /
    HUP / PIPE dispositions even though the HOST ignores them.

    Before the fix the pty child inherited SIG_IGN (set by the host) and
    ignored dispositions survive exec — bash kept TERM ignored for its jobs,
    so a normal stop's per-job SIGTERM was a silent no-op (the 2026-09-09
    field state). The suite guards os.execvp in-process, so the probe is a
    fake exec that snapshots the dispositions the real exec would carry.
    """
    import base.sessions.pty.host as host_mod
    from base.sessions.pty.launch import _fork_shell

    probe_file = tmp_path / "dispositions.txt"

    def disposition_name(sig: signal.Signals) -> str:
        value = signal.getsignal(sig)
        return value.name if isinstance(value, signal.Handlers) else "custom"

    def fake_execvp(file: str, args: list[str]) -> object:
        # The real exec would carry exactly these dispositions into bash.
        probe_file.write_text(
            f"TERM={disposition_name(signal.SIGTERM)} "
            f"HUP={disposition_name(signal.SIGHUP)} "
            f"PIPE={disposition_name(signal.SIGPIPE)}\n",
            encoding="utf-8",
        )
        os._exit(0)

    monkeypatch.setattr(host_mod.os, "execvp", fake_execvp)
    # Mimic host.main(): the host ignores these three signals so a stray
    # signal aimed at the session tree cannot take the host down.
    watched = (signal.SIGHUP, signal.SIGTERM, signal.SIGPIPE)
    previous = {sig: signal.getsignal(sig) for sig in watched}
    for sig in watched:
        signal.signal(sig, signal.SIG_IGN)
    master: int | None = None
    try:
        pid, master = _fork_shell(str(tmp_path), {}, 80, 24)
        deadline = time.monotonic() + 8
        status = None
        while time.monotonic() < deadline:
            try:
                got, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if got:
                break
            time.sleep(0.05)
        assert status is not None, "the forked child never reached the exec"
        assert os.waitstatus_to_exitcode(status) == 0, "the probe exec ran the exit path"
        probe = probe_file.read_text(encoding="utf-8")
        assert "TERM=SIG_DFL" in probe, f"TERM must be default in the child: {probe}"
        assert "HUP=SIG_DFL" in probe, f"HUP must be default in the child: {probe}"
        assert "PIPE=SIG_DFL" in probe, f"PIPE must be default in the child: {probe}"
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if master is not None:
            os.close(master)


@pytest.mark.flaky
def test_stop_closes_busy_terminal_job_with_real_signals(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """A regular interruptible foreground job closes via a normal stop."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    name = "ava-agent-987-shell-2045-job"
    shell = _start_busy_session(terminal, home, name, _TERM_OK_JOB, pty_reaper)
    # wait for the job to actually start (bash prompt readiness + submit)
    jobs = _started_jobs(shell, pty_reaper)
    assert jobs, "the synthetic job never started"

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=10) == 0
    assert not terminal.has_session(name)
    assert _wait_exit(jobs[0].pid), "the job must be closed by the normal stop"


@pytest.mark.flaky
def test_stop_terminates_restart_loop_shell(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """A shell loop that keeps respawning its job cannot outlive the stop.

    HUP goes to the shell first (stopping the spawner), then the captured
    descendants get their TERM — the ordering proven in the field (#2045).
    """
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    name = "ava-agent-987-shell-2045-loop"
    assert terminal.new_session(name, _LOOP_SHELL, home, env={"AVA_HOME": str(home)})
    shell = pty_reaper.track_session(name)
    assert _started_jobs(shell, pty_reaper), "the loop never spawned its first child"

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=15) == 0
    assert not terminal.has_session(name)
    assert _wait_exit(shell.pid), "the restart loop shell must exit"


@pytest.mark.flaky
def test_stop_kills_a_job_that_ignores_termination_after_its_grace(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """A job that ignores TERM and HUP is SIGKILLed once the bounded grace ends:
    the stop completes well inside its deadline, and the owner still gets the
    closure notice for the work the kill cut short."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    monkeypatch.setattr(strict, "_TERMINAL_STOP_GRACE_S", 1.0)
    name = "ava-agent-987-shell-2045-stubborn"
    shell = _start_busy_session(terminal, home, name, _STUBBORN_JOB, pty_reaper)
    jobs = _started_jobs(shell, pty_reaper)
    assert jobs, "the stubborn job never started"

    started = time.monotonic()
    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=12) == 0
    assert time.monotonic() - started < 8, "the grace bounds the wait, not the stop deadline"
    assert _wait_exit(jobs[0].pid, timeout=5), "the job outlived the stop"
    assert not terminal.has_session(name)
    assert len(_notice_files(home)) == 1, "a killed busy session still leaves its notice"


@pytest.mark.flaky
def test_stop_kills_a_double_forked_orphan_of_the_session(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """A process that double-forked out of the shell's tree stays in its POSIX
    session: the stop captures it before the hangup, and it dies with the rest
    instead of outliving a stop that reported success."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    monkeypatch.setattr(strict, "_TERMINAL_STOP_GRACE_S", 1.0)
    name = "ava-agent-987-shell-2046-orphan"
    pidfile = home / "orphan.pid"
    job = _double_forked_job(pidfile, ignore=("SIGTERM", "SIGHUP"))
    shell = _start_busy_session(terminal, home, name, job, pty_reaper)
    orphan = _session_orphan(pidfile, shell, pty_reaper)

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=12) == 0
    assert _wait_exit(orphan.pid, timeout=5), "the orphan outlived a stop that succeeded"
    assert len(_notice_files(home)) == 1, "the orphan was running work: the session was busy"


@pytest.mark.flaky
def test_stop_terminates_a_double_forked_job_outside_the_shell_tree(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """The cancel reaches the whole session: TERM finds a hangup-ignoring
    worker that left the shell's tree through the shell's POSIX session."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    name = "ava-agent-987-shell-2045-orphan"
    pidfile = home / "orphan.pid"
    job = _double_forked_job(pidfile, ignore=("SIGHUP",))
    shell = _start_busy_session(terminal, home, name, job, pty_reaper)
    orphan = _session_orphan(pidfile, shell, pty_reaper)

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=15) == 0
    assert _wait_exit(orphan.pid, timeout=1), "the stop left the session's worker running"


@pytest.mark.flaky
@pytest.mark.parametrize("disposition", ["SIG_IGN", "SIG_DFL"])
def test_stop_kills_a_helper_its_job_forks_on_term_and_orphans(
    disposition: str, home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """The job's TERM handler forks a helper and exits at once: no captured
    process is left alive to lead the stop to the helper (with SIG_DFL it never
    even receives a signal), but it is still in the shell's POSIX session. The
    stop takes it — it succeeds only with the helper dead."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    monkeypatch.setattr(strict, "_TERMINAL_STOP_GRACE_S", 1.0)
    name = "ava-agent-987-shell-2047-helper"
    pidfile = home / "helper.pid"
    script = home / f"{name}.job.py"
    script.write_text(_FORK_ON_TERM_JOB.format(disposition=disposition), encoding="utf-8")
    assert terminal.new_session(
        name, f"python3 -u {script} {pidfile}", home, env={"AVA_HOME": str(home)}
    )
    shell = pty_reaper.track_session(name)
    assert _started_jobs(shell, pty_reaper), "the job never started"
    ready = Path(f"{pidfile}.ready")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not ready.exists():
        time.sleep(0.05)
    assert ready.exists(), "the job never installed its TERM handler"

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=12) == 0
    assert pidfile.exists(), "the job's TERM handler never forked its helper"
    helper = int(pidfile.read_text(encoding="utf-8"))
    with contextlib.suppress(psutil.NoSuchProcess):
        pty_reaper.track(psutil.Process(helper))
    assert _wait_exit(helper, timeout=5), "the helper outlived a stop that succeeded"
    assert len(_notice_files(home)) == 1


@pytest.mark.flaky
@pytest.mark.parametrize(
    ("hop_ms", "hops", "grace_s"), [(3, 60, 2.0), (8, 40, 2.0), (10, 150, 2.0), (10, 150, 0.5)]
)
def test_stop_kills_the_last_hop_of_a_fork_chain_started_on_term(
    hop_ms: int,
    hops: int,
    grace_s: float,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
) -> None:
    """A TERM handler starts a chain of processes that each fork the next and
    exit within a few ms — too short for a full process-table pass to read one
    alive. While the session still holds a process the grace keeps polling, a
    proven pass that reads a hop keeps the proof current, and no part of the
    chain outlives the stop: it either reached its last hop, which dies with
    the stop, or the stop cut it short. 10 ms x 150 runs past the proof's first
    second; with a 0.5 s grace it is still forking when the kill starts, whose
    freeze passes must stop a hop before it forks on."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    monkeypatch.setattr(strict, "_TERMINAL_STOP_GRACE_S", grace_s)
    name = "ava-agent-987-shell-2048-chain"
    pidfile = home / "last.pid"
    script = home / f"{name}.job.py"
    script.write_text(_FORK_CHAIN_JOB.format(hop_ms=hop_ms, hops=hops), encoding="utf-8")
    assert terminal.new_session(
        name, f"python3 -u {script} {pidfile}", home, env={"AVA_HOME": str(home)}
    )
    shell = pty_reaper.track_session(name)
    assert _started_jobs(shell, pty_reaper), "the job never started"
    ready = Path(f"{pidfile}.ready")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not ready.exists():
        time.sleep(0.05)
    assert ready.exists(), "the job never installed its TERM handler"

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=12) == 0
    # Wait out the chain's own run: a chain that escaped always has a live hop
    # (each hop forks the next before it exits), or has reached its last one.
    time.sleep(hops * hop_ms / 1000 + 1.0)
    left = _running(script)
    assert not left, f"part of the chain outlived a stop that succeeded: {left}"
    if pidfile.exists():
        assert _wait_exit(int(pidfile.read_text(encoding="utf-8")), timeout=5)
    assert len(_notice_files(home)) == 1


@pytest.mark.flaky
def test_incomplete_stop_still_records_the_sessions_it_closed(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pty_reaper: PtyReaper,
) -> None:
    """One session closes; in another the shell itself outlives the stop and
    keeps its job through the SIGKILL. The stop is incomplete, yet the closed
    session's notice is recorded before it reports — its record is gone, so a
    retry could never record it. The session whose shell still lives records
    nothing and is left to the retry, which records it: each notice once."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    monkeypatch.setattr(strict, "_TERMINAL_STOP_GRACE_S", 0.5)
    closed = "ava-agent-987-shell-2051-closed"
    stuck = "ava-agent-987-shell-2052-stuck"
    closed_shell = _start_busy_session(terminal, home, closed, _STUBBORN_JOB, pty_reaper)
    script = home / f"{stuck}.job.py"
    script.write_text(_STUBBORN_JOB, encoding="utf-8")
    # The shell ignores its hangup too, so the unclosed session outlives the stop.
    assert terminal.new_session(
        stuck, f"trap '' HUP; python3 -u {script}", home, env={"AVA_HOME": str(home)}
    )
    stuck_shell = pty_reaper.track_session(stuck)
    assert _started_jobs(closed_shell, pty_reaper), "the closed session's job never started"
    assert _started_jobs(stuck_shell, pty_reaper), "the stuck session's job never started"
    real_kill = session_tree.kill_session_tree

    def kill(leader: OwnedProcess, **kwargs: Any) -> session_tree.TreeKill:
        if leader == stuck_shell:
            also = tuple(kwargs["also"])
            return session_tree.TreeKill((), also, also)
        return real_kill(leader, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(session_tree, "kill_session_tree", kill)
        assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=12) == 1
    assert admission.held(), "the hold must survive an incomplete stop"
    assert stuck in capsys.readouterr().err
    assert _notice_names(home) == [closed], "the closed session's notice was lost"
    assert "survivors" not in _notices(home)[0], "nothing of the closed session survived"

    def still_drained(_timeout: float, **_kw: object) -> None:
        """The retry re-enters the held stop; the drain stand-in only opens a fresh hold."""

    monkeypatch.setattr(command, "pause_agents", still_drained)
    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=12) == 0
    assert _notice_names(home) == sorted([closed, stuck])


def test_a_terminal_left_after_the_closure_fails_the_stop_in_its_own_words(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal still present once the stop closed every captured session
    fails the stop — naming it, and without maintenance's claim that nothing
    will be killed: this stop has just killed its sessions."""
    name = "ava-agent-987-shell-2053-lingering"
    listing = SimpleNamespace(list_sessions=lambda: [name])
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setattr(strict, "get_shell_backend", lambda: listing)

    with pytest.raises(StopIncompleteError) as excinfo:
        strict._await_no_terminals(time.monotonic(), "terminals")
    message = str(excinfo.value)
    assert name in message
    assert "will not kill" not in message
    assert excinfo.value.stage == "terminals"


def test_a_terminal_that_clears_within_the_stop_deadline_does_not_fail_the_stop(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closure evidence waits until the stop's deadline, and at least the
    SIGKILL leg's bound: a PTY host still tearing down past that bound but
    clearing its record before the deadline is a closed terminal, not a
    failed stop."""
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setattr(strict, "_TERMINAL_KILL_WAIT_S", 0.2)
    monkeypatch.setattr(strict, "capture_terminals", lambda: strict.TerminalInventory(()))
    cleared_at = time.monotonic() + 0.6
    name = "ava-agent-987-shell-2053-tearing-down"

    def tearing_down() -> list[str]:
        return [] if time.monotonic() >= cleared_at else [name]

    monkeypatch.setattr(strict, "live_terminals", tearing_down)
    strict.close_terminals(time.monotonic() + 10, "stop-test", datetime.now(UTC))
    assert time.monotonic() >= cleared_at


def _running(script: Path) -> list[int]:
    """Live processes running `script` (a job and every fork of it carry it on argv)."""
    return [
        process.pid
        for process in psutil.process_iter(["cmdline"])
        if str(script) in (process.info["cmdline"] or ()) and not _has_exited(process)
    ]


def _notice_files(home: Path) -> list[Path]:
    journal = pty_close_notices.journal_dir()
    return list(journal.iterdir()) if journal.is_dir() else []


def _notices(home: Path) -> list[dict[str, Any]]:
    return sorted(
        (json.loads(path.read_text()) for path in _notice_files(home)),
        key=lambda notice: str(notice["name"]),
    )


def _notice_names(home: Path) -> list[str]:
    return [notice["name"] for notice in _notices(home)]


def test_stop_records_notice_for_verified_closed_busy_session(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pty_reaper: PtyReaper,
) -> None:
    """A busy session verified closed leaves one durable notice; delivery to
    its owner happens at the next ops-daemon startup (issue #2044)."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    name = "ava-agent-987-shell-2044-busy"
    shell = _start_busy_session(terminal, home, name, _TERM_OK_JOB, pty_reaper)
    assert _started_jobs(shell, pty_reaper), "the job never started"

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=15) == 0
    files = _notice_files(home)
    assert len(files) == 1
    import json as _json

    notice = _json.loads(files[0].read_text())
    assert notice["agent_id"] == 987
    assert notice["session_id"] == 2044
    assert notice["name"] == name
    assert notice["machine"]
    assert notice["operation"]
    assert "operator stop" in notice["reason"]


def test_stop_records_nothing_for_idle_shell(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """An idle shell (no jobs) closed by stop is silent — the TTL reaper's
    quiet-empty policy, never a blanket close notification (issue #2044 #3).

    The session has no initial command: the host types one into the login
    shell after its prompt, where it runs as a job. Idle is the login shell at
    its first prompt — bash prints it only after its startup files ran. The
    test owns that prompt: the login shell reads this home's `.bash_profile`
    after the system's files, so the prompt is `_IDLE_PROMPT` whatever the
    host's or the developer's shell configuration prints. The machine name
    lets a wrongly recorded notice land in the journal.
    """
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    (home / ".bash_profile").write_text(f"PS1='{_IDLE_PROMPT} '\n")
    name = "ava-agent-987-shell-2044-idle"
    env = {"AVA_HOME": str(home), "HOME": str(home)}
    assert terminal.new_session(name, "", home, env=env)
    shell = pty_reaper.track_session(name)
    deadline = time.monotonic() + 15
    while not terminal.capture_pane(name).rstrip().endswith(_IDLE_PROMPT):
        assert time.monotonic() < deadline, "the login shell never printed its prompt"
        time.sleep(0.1)
    members = [member.pid for member in session_tree.session_members(shell)]
    assert members == [shell.pid], "precondition: the shell at its prompt runs no job"

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=15) == 0
    assert _notice_files(home) == []


def test_stop_keeps_hold_when_a_process_outlives_the_kill(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pty_reaper: PtyReaper,
) -> None:
    """The kill ends the shell, but a job outlives its SIGKILL (another user's,
    which this stop may not signal). The stop is incomplete — the hold stays
    and the failure names the phase and the session — yet the session is over
    for its owner: the shell is verified gone, so its notice is recorded, naming
    the process left running. A retry no longer sees the session and adds no
    second notice."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    monkeypatch.setattr(strict, "_TERMINAL_STOP_GRACE_S", 0.5)
    name = "ava-agent-987-shell-2044-stubborn"
    shell = _start_busy_session(terminal, home, name, _STUBBORN_JOB, pty_reaper)
    jobs = _started_jobs(shell, pty_reaper)
    assert jobs, "the stubborn job never started"

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(session_tree, "kill_session_tree", _denied_but_the_shell)
        assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=12) == 1
    assert admission.held(), "the hold must survive an incomplete stop"
    assert psutil.pid_exists(jobs[0].pid)
    err = capsys.readouterr().err
    assert "terminals" in err, "the failure must name the phase"
    assert name in err, "the failure must name the owning session"
    assert "nothing was force-killed" not in err, "the survivors outlived a SIGKILL"
    notices = _notices(home)
    assert [notice["name"] for notice in notices] == [name]
    assert notices[0]["survivors"] == [{"pid": jobs[0].pid, "name": jobs[0].name()}]

    def still_drained(_timeout: float, **_kw: object) -> None:
        """The retry re-enters the held stop; the drain stand-in only opens a fresh hold."""

    monkeypatch.setattr(command, "pause_agents", still_drained)
    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=12) == 0
    assert len(_notice_files(home)) == 1, "the retry recorded the closed session again"


@pytest.mark.flaky
def test_stop_tolerates_naturally_exited_session_with_stale_record(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """A session whose shell exited naturally before stop (leaving a record)
    must not fail the stop with RuntimeError — the terminal is already gone."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    name = "ava-agent-987-shell-2045-already-dead"
    shell = _start_busy_session(terminal, home, name, _TERM_OK_JOB, pty_reaper)
    # Kill the shell process out-of-band to leave its session record on disk
    os.kill(shell.pid, signal.SIGKILL)
    assert _wait_exit(shell.pid), "the shell must terminate after SIGKILL"
    # The record on disk remains; normal stop must tolerate the dead session
    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=10) == 0


def test_wait_exit_reads_a_reaped_process_as_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process reaped between its construction and the status read raises
    NoSuchProcess from the read itself (task #4397 — the shape that evicted
    #3142 from the merge queue); the wait must read that as an exited process."""

    def vanished(*_args: object, **_kwargs: object) -> None:
        raise psutil.NoSuchProcess(0)

    monkeypatch.setattr(psutil.Process, "status", vanished)
    assert _wait_exit(os.getpid(), timeout=0.3) is True
