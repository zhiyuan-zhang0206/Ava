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
  spawning new jobs), then TERMs the captured jobs, and a survivor leaves the
  maintenance hold in place with a per-phase diagnostic — never a force.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import psutil
import pytest

from cli.commands import _temporary_stop as command
from cli.commands import stop as entry
from cli.commands._maintenance_stop import OwnedProcess
from shared import maintenance
from shared.paths import run_dir
from shared.session_backend import PtySessionBackend
from shared.session_record import SessionRecord
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


def _wait_exit(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return True
        if process.status() == psutil.STATUS_ZOMBIE:
            return True
        time.sleep(0.05)
    return False


def _shell_children(shell: OwnedProcess) -> list[psutil.Process]:
    return psutil.Process(shell.pid).children(recursive=True)


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
    terminal: PtySessionBackend, home: Path, name: str, job: str
) -> OwnedProcess:
    # The job lives in a script file: shell quoting of an inline -c program is
    # the flakiest part of the fixture, and the production jobs (watchers) are
    # file-backed the same way.
    script = home / f"{name}.job.py"
    script.write_text(job, encoding="utf-8")
    assert terminal.new_session(name, f"python3 -u {script}", home, env={"AVA_HOME": str(home)})
    record = SessionRecord.read(run_dir() / "pty" / f"{name}.json")
    assert record is not None
    shell = OwnedProcess(record.pid, record.create_time, record.starttime)
    assert shell.live()
    return shell


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
    import shared.pty_sessions.host as host_mod
    from shared.pty_sessions.host import _fork_shell

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
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regular interruptible foreground job closes via a normal stop."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    name = "ava-agent-987-shell-2045-job"
    shell = _start_busy_session(terminal, home, name, _TERM_OK_JOB)
    try:
        # wait for the job to actually start (bash prompt readiness + submit)
        jobs: list[int] = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not jobs:
            jobs = [
                child.pid
                for child in _shell_children(shell)
                if child.status() != psutil.STATUS_ZOMBIE
            ]
            time.sleep(0.1)
        assert jobs, "the synthetic job never started"
        job_pid = jobs[0]

        assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=10) == 0
        assert not terminal.has_session(name)
        assert _wait_exit(job_pid), "the job must be closed by the normal stop"
    finally:
        terminal.kill_session(name)


@pytest.mark.flaky
def test_stop_terminates_restart_loop_shell(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A shell loop that keeps respawning its job cannot outlive the stop.

    HUP goes to the shell first (stopping the spawner), then the captured
    descendants get their TERM — the ordering proven in the field (#2045).
    """
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    name = "ava-agent-987-shell-2045-loop"
    assert terminal.new_session(name, _LOOP_SHELL, home, env={"AVA_HOME": str(home)})
    record = SessionRecord.read(run_dir() / "pty" / f"{name}.json")
    assert record is not None
    shell = OwnedProcess(record.pid, record.create_time, record.starttime)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not _shell_children(shell):
            time.sleep(0.1)
        assert _shell_children(shell), "the loop never spawned its first child"

        assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=15) == 0
        assert not terminal.has_session(name)
        assert _wait_exit(shell.pid), "the restart loop shell must exit"
    finally:
        terminal.kill_session(name)


@pytest.mark.flaky
def test_stop_keeps_hold_when_job_ignores_termination(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A job that ignores TERM and HUP: bounded timeout, hold kept, no force."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    name = "ava-agent-987-shell-2045-stubborn"
    shell = _start_busy_session(terminal, home, name, _STUBBORN_JOB)
    try:
        jobs: list[int] = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not jobs:
            jobs = [
                child.pid
                for child in _shell_children(shell)
                if child.status() != psutil.STATUS_ZOMBIE
            ]
            time.sleep(0.1)
        assert jobs, "the stubborn job never started"
        job_pid = jobs[0]

        assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=1) == 1
        assert maintenance.held(), "the hold must survive an incomplete stop"
        # The shell got a normal HUP (not a force) and may have exited,
        # dropping its record; the signal-ignoring job must still be alive.
        assert psutil.pid_exists(job_pid), "no kill escalation: the job is still there"
        err = capsys.readouterr().err
        assert "terminals" in err, "the failure must name the phase that ran out"
        assert name in err, "the failure must name the owning session"
    finally:
        terminal.kill_session(name)


def _notice_files(home: Path) -> list[Path]:
    from ops import pty_close_notices

    journal = pty_close_notices.journal_dir()
    return list(journal.iterdir()) if journal.is_dir() else []


def test_stop_records_notice_for_verified_closed_busy_session(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A busy session verified closed leaves one durable notice; delivery to
    its owner happens at the next ops-daemon startup (issue #2044)."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    (home / "machine_name").write_text("test-host")
    name = "ava-agent-987-shell-2044-busy"
    shell = _start_busy_session(terminal, home, name, _TERM_OK_JOB)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not _shell_children(shell):
            time.sleep(0.1)
        assert _shell_children(shell), "the job never started"

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
    finally:
        terminal.kill_session(name)


def test_stop_records_nothing_for_idle_shell(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle shell (no jobs) closed by stop is silent — the TTL reaper's
    quiet-empty policy, never a blanket close notification (issue #2044 #3)."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    name = "ava-agent-987-shell-2044-idle"
    assert terminal.new_session(name, "bash --norc", home, env={"AVA_HOME": str(home)})
    record = SessionRecord.read(run_dir() / "pty" / f"{name}.json")
    assert record is not None
    shell = OwnedProcess(record.pid, record.create_time, record.starttime)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and _shell_children(shell):
            time.sleep(0.1)
        assert not _shell_children(shell), "the idle shell spawned children"

        assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=15) == 0
        assert _notice_files(home) == []
    finally:
        terminal.kill_session(name)


def test_stop_records_nothing_on_timeout(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stop that timed out never claims a closure — only verified exits are
    recorded, partial success records nothing (issue #2044 #2)."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    name = "ava-agent-987-shell-2044-stubborn"
    shell = _start_busy_session(terminal, home, name, _STUBBORN_JOB)
    try:
        jobs: list[int] = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not jobs:
            jobs = [
                child.pid
                for child in _shell_children(shell)
                if child.status() != psutil.STATUS_ZOMBIE
            ]
            time.sleep(0.1)
        assert jobs, "the stubborn job never started"

        assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=1) == 1
        assert "terminals" in capsys.readouterr().err
        assert _notice_files(home) == []
    finally:
        terminal.kill_session(name)
