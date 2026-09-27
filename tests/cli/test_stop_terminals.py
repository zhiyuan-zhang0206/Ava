"""Normal-stop terminal closure (#2045): real PTY sessions, real signals.

The 2026-09-09 migration stop timed out on machines whose services had all
exited: the survivors were persistent-shell jobs (watcher / tee) that ignored
SIGTERM. Root cause: the PTY host ignores SIGHUP/SIGTERM/SIGPIPE, and ignored
dispositions survive exec — the interactive bash inherited SIG_IGN and kept it
ignored for itself and every job, so the stop path's per-PID SIGTERM was a
no-op. These tests lock the two parts of the fix:

- the pty child resets the dispositions before exec (TERM/HUP reach the shell
  and its jobs again),
- `_stop_terminals` HUPs the shells first (stopping restart loops from
  spawning new jobs), then TERMs the rest of each session's captured
  membership.

What outlives a bounded grace is SIGKILLed with its whole session — a
double-forked orphan included (decisions/2026-09-28-stop-escalates-to-sigkill.md);
only a process that outlives its SIGKILL leaves the maintenance hold in place
with a per-phase diagnostic.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from cli.commands import _temporary_stop as command
from cli.commands import stop as entry
from cli.commands._maintenance_stop import OwnedProcess
from cli.commands._maintenance_stop_report import StopIncompleteError
from shared import maintenance
from shared.session_backend import PtySessionBackend
from shared.sessions.pty import session_tree
from tests.cli.conftest import PtyReaper
from tests.cli.test_pause_stop import dependencies
from tests.cli.test_pause_stop import home as home

# A regular interruptible job: NO signal handlers of its own — it relies on
# the default TERM disposition. Before the fix it inherited SIG_IGN through
# bash (ignored dispositions survive exec), so TERM never reached it.
_TERM_OK_JOB = "import time\nprint('job-ready', flush=True)\nwhile True: time.sleep(0.1)\n"

_STUBBORN_JOB = (
    "import signal,time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "print('stubborn-ready', flush=True)\n"
    "while True: time.sleep(0.1)\n"
)

_LOOP_SHELL = "bash -c 'while true; do sleep 1; done'"

# Double fork out of the shell's tree: the grandchild is reparented to init but
# stays in the shell's POSIX session, and it ignores the stop's HUP and TERM.
_DOUBLE_FORK_JOB = (
    "import os,signal,sys,time\n"
    "if os.fork() == 0:\n"
    "    if os.fork() == 0:\n"
    "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "        signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "        open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
    "        os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "        while True: time.sleep(0.1)\n"
    "    os._exit(0)\n"
    "os.wait()\n"
)

# A foreground job whose TERM handler forks a helper and exits at once. The
# helper is born after the stop's signals and its parent is gone before the next
# poll; the shell has already died of its hangup. It keeps the shell's POSIX
# session. `{disposition}` is the helper's own HUP/TERM disposition.
_FORK_ON_TERM_JOB = (
    "import os,signal,sys,time\n"
    "def on_term(*_):\n"
    "    if os.fork() == 0:\n"
    "        signal.signal(signal.SIGTERM, signal.{disposition})\n"
    "        signal.signal(signal.SIGHUP, signal.{disposition})\n"
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
    monkeypatch.setenv("AVA_HOME_OVERRIDE", "1")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(command, "get_shell_backend", lambda: terminal)
    from cli.commands import _maintenance_stop as strict

    monkeypatch.setattr(strict, "get_shell_backend", lambda: terminal)
    for name in ("stop_gate_service", "stop_permissions_helper", "stop_lgtm_services"):
        monkeypatch.setattr(f"cli.commands._stop_extras.{name}", lambda **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
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
    import shared.sessions.pty.host as host_mod
    from shared.sessions.pty.launch import _fork_shell

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
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
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
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGPIPE):
            signal.signal(sig, signal.SIG_DFL)
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
    monkeypatch.setattr(command, "_TERMINAL_STOP_GRACE_S", 1.0)
    (home / "machine_name").write_text("test-host")
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
    monkeypatch.setattr(command, "_TERMINAL_STOP_GRACE_S", 1.0)
    (home / "machine_name").write_text("test-host")
    name = "ava-agent-987-shell-2046-orphan"
    pidfile = home / "orphan.pid"
    script = home / f"{name}.job.py"
    script.write_text(_DOUBLE_FORK_JOB, encoding="utf-8")
    assert terminal.new_session(
        name, f"python3 -u {script} {pidfile}", home, env={"AVA_HOME": str(home)}
    )
    shell = pty_reaper.track_session(name)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not pidfile.exists():
        time.sleep(0.1)
    assert pidfile.exists(), "the orphan never started"
    orphan = psutil.Process(int(pidfile.read_text(encoding="utf-8")))
    pty_reaper.track(orphan)
    assert orphan.pid not in {child.pid for child in _shell_children(shell)}
    assert os.getsid(orphan.pid) == shell.pid, "precondition: it is in the shell's session"

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=12) == 0
    assert _wait_exit(orphan.pid, timeout=5), "the orphan outlived a stop that succeeded"
    assert len(_notice_files(home)) == 1, "the orphan was running work: the session was busy"


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
    monkeypatch.setattr(command, "_TERMINAL_STOP_GRACE_S", 1.0)
    (home / "machine_name").write_text("test-host")
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
def test_incomplete_stop_still_records_the_sessions_it_closed(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pty_reaper: PtyReaper,
) -> None:
    """One session closes; another keeps its processes through the SIGKILL
    (this user may not signal them). The stop is incomplete, yet the closed
    session's notice is recorded before it reports — its record is gone, so a
    retry could never record it. The unclosed session is left to the retry,
    which records it: each notice exactly once."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    monkeypatch.setattr(command, "_TERMINAL_STOP_GRACE_S", 0.5)
    (home / "machine_name").write_text("test-host")
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
    assert maintenance.held(), "the hold must survive an incomplete stop"
    assert stuck in capsys.readouterr().err
    assert _notice_names(home) == [closed], "the closed session's notice was lost"

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
    monkeypatch.setenv("AVA_HOME_OVERRIDE", "1")
    from cli.commands import _maintenance_stop as strict

    monkeypatch.setattr(strict, "get_shell_backend", lambda: listing)

    with pytest.raises(StopIncompleteError) as excinfo:
        command._await_no_terminals(time.monotonic())
    message = str(excinfo.value)
    assert name in message
    assert "will not kill" not in message
    assert excinfo.value.stage == "terminals"


def _notice_files(home: Path) -> list[Path]:
    from ops import pty_close_notices

    journal = pty_close_notices.journal_dir()
    return list(journal.iterdir()) if journal.is_dir() else []


def _notice_names(home: Path) -> list[str]:
    import json as _json

    return sorted(_json.loads(path.read_text())["name"] for path in _notice_files(home))


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
    (home / "machine_name").write_text("test-host")
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
    quiet-empty policy, never a blanket close notification (issue #2044 #3)."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    name = "ava-agent-987-shell-2044-idle"
    assert terminal.new_session(name, "bash --norc", home, env={"AVA_HOME": str(home)})
    shell = pty_reaper.track_session(name)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _shell_children(shell):
        time.sleep(0.1)
    assert not _shell_children(shell), "the idle shell spawned children"

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=15) == 0
    assert _notice_files(home) == []


def test_stop_keeps_hold_when_a_process_outlives_the_kill(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pty_reaper: PtyReaper,
) -> None:
    """A process that outlives its SIGKILL (another user's, which this stop may
    not signal) leaves the stop incomplete: the hold stays, the failure names
    the phase and the session, and no closure is claimed — only verified exits
    are recorded (issue #2044 #2)."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    monkeypatch.setattr(command, "_TERMINAL_STOP_GRACE_S", 0.5)

    def unkillable(
        leader: OwnedProcess,
        *,
        also: tuple[OwnedProcess, ...] = (),
        wait_s: float,
        proven_at: float | None = None,
    ) -> session_tree.TreeKill:
        del leader, wait_s, proven_at
        return session_tree.TreeKill((), tuple(also), tuple(also))

    monkeypatch.setattr(session_tree, "kill_session_tree", unkillable)
    name = "ava-agent-987-shell-2044-stubborn"
    shell = _start_busy_session(terminal, home, name, _STUBBORN_JOB, pty_reaper)
    jobs = _started_jobs(shell, pty_reaper)
    assert jobs, "the stubborn job never started"

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=12) == 1
    assert maintenance.held(), "the hold must survive an incomplete stop"
    assert psutil.pid_exists(jobs[0].pid)
    err = capsys.readouterr().err
    assert "terminals" in err, "the failure must name the phase"
    assert name in err, "the failure must name the owning session"
    assert "nothing was force-killed" not in err, "the survivors outlived a SIGKILL"
    assert _notice_files(home) == []


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
