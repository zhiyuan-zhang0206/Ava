"""Terminal closure at a normal stop (#2045) and a release: real PTYs, real signals.

The 2026-09-09 migration stop timed out on machines whose services had all
exited: the survivors were persistent-shell jobs (watcher / tee) that ignored
SIGTERM. Root cause: the PTY host ignores SIGHUP/SIGTERM/SIGPIPE, and ignored
dispositions survive exec — the interactive bash inherited SIG_IGN and kept it
ignored for itself and every job, so the stop path's per-PID SIGTERM was a
no-op. These tests lock the two parts of the fix:

- the pty child resets the dispositions before exec (TERM/HUP reach the shell
  and its jobs again),
- `close_terminals` HUPs the shells first (stopping restart loops from
  spawning new jobs), then TERMs the captured jobs, and a survivor leaves the
  maintenance hold in place with a per-phase diagnostic — never a force.

A release is different (decisions/2026-09-27-fleet-release-and-cutover-policies.md
item 2): terminals get a bounded completed-work wait, then the same cancel,
then SIGKILL over their captured births — none survives, and each busy owner
gets the `ava stop` closure notice naming the release.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psutil
import pytest

from cli.commands import maintenance_stop as strict
from cli.commands import stop as entry
from cli.commands._maintenance_stop_report import StopIncompleteError
from cli.commands.maintenance_stop import OwnedProcess
from ops import pty_close_notices
from shared import maintenance
from shared.session_backend import PtySessionBackend
from tests.agent.test_maintenance import WHEN
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
    monkeypatch.setattr(strict, "get_shell_backend", lambda: terminal)
    for name in ("stop_permissions_helper",):
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
def test_stop_keeps_hold_when_job_ignores_termination(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pty_reaper: PtyReaper,
) -> None:
    """A job that ignores TERM and HUP: bounded timeout, hold kept, no force."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    name = "ava-agent-987-shell-2045-stubborn"
    shell = _start_busy_session(terminal, home, name, _STUBBORN_JOB, pty_reaper)
    jobs = _started_jobs(shell, pty_reaper)
    assert jobs, "the stubborn job never started"

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=1) == 1
    assert maintenance.held(), "the hold must survive an incomplete stop"
    # The shell got a normal HUP (not a force) and may have exited,
    # dropping its record; the signal-ignoring job must still be alive.
    assert psutil.pid_exists(jobs[0].pid), "no kill escalation: the job is still there"
    err = capsys.readouterr().err
    assert "terminals" in err, "the failure must name the phase that ran out"
    assert name in err, "the failure must name the owning session"


def _notice_files(home: Path) -> list[Path]:
    from ops import pty_close_notices

    journal = pty_close_notices.journal_dir()
    return list(journal.iterdir()) if journal.is_dir() else []


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


def test_stop_records_nothing_on_timeout(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pty_reaper: PtyReaper,
) -> None:
    """A stop that timed out never claims a closure — only verified exits are
    recorded, partial success records nothing (issue #2044 #2)."""
    dependencies(monkeypatch)
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    name = "ava-agent-987-shell-2044-stubborn"
    shell = _start_busy_session(terminal, home, name, _STUBBORN_JOB, pty_reaper)
    assert _started_jobs(shell, pty_reaper), "the stubborn job never started"

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=1) == 1
    assert "terminals" in capsys.readouterr().err
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


# ─── release boundary (FC-6) ─────────────────────────────────────────────────


def _finishing_job(marker: Path) -> str:
    return (
        "import pathlib,time\n"
        "print('finishing-ready', flush=True)\n"
        "time.sleep(2.0)\n"
        f"pathlib.Path({str(marker)!r}).write_text('done')\n"
    )


def _release_notices(home: Path) -> list[dict[str, object]]:
    return [json.loads(path.read_text()) for path in _notice_files(home)]


def test_release_work_wait_lets_a_finishing_job_complete_then_closes_it_idle(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """The completed-work bound signals nothing: a job that finishes in time
    is never interrupted, and its session closes idle, without a notice."""
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    marker = home / "finished"
    name = "ava-agent-987-shell-6001-finishing"
    shell = _start_busy_session(terminal, home, name, _finishing_job(marker), pty_reaper)
    assert _started_jobs(shell, pty_reaper), "the finishing job never started"

    assert strict.await_terminal_work(20) == []
    assert marker.read_text() == "done", "the job ran to completion"
    closed = strict.close_release_terminals(
        str(uuid4()), WHEN, grace_s=10, kill_s=10, reason=pty_close_notices.RELEASE_REASON
    )
    assert sorted(closed.shells) == [name] and closed.busy == {}
    assert not terminal.has_session(name)
    assert _wait_exit(shell.pid)
    assert _notice_files(home) == []


def test_release_work_wait_is_bounded_and_interrupts_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """A resident job (a schedule runner, a coding tool) only spends the bound."""
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    name = "ava-agent-987-shell-6002-resident"
    shell = _start_busy_session(terminal, home, name, _TERM_OK_JOB, pty_reaper)
    jobs = _started_jobs(shell, pty_reaper)
    assert jobs, "the resident job never started"

    started = time.monotonic()
    assert strict.await_terminal_work(1.0) == [name]
    assert 1.0 <= time.monotonic() - started < 10
    assert not _has_exited(jobs[0]) and shell.live(), "the wait never signals"


def test_release_closure_kills_a_job_that_ignores_the_cancel_and_notifies_owner(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """No terminal survives a release: a job ignoring HUP and TERM gets SIGKILL
    over its captured birth, and its owner gets the `ava stop` notice naming
    the release."""
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    (home / "machine_name").write_text("test-host")
    name = "ava-agent-987-shell-6003-stubborn"
    shell = _start_busy_session(terminal, home, name, _STUBBORN_JOB, pty_reaper)
    jobs = _started_jobs(shell, pty_reaper)
    assert jobs, "the stubborn job never started"
    operation = str(uuid4())

    closed = strict.close_release_terminals(
        operation, WHEN, grace_s=0.5, kill_s=10, reason=pty_close_notices.RELEASE_REASON
    )
    assert sorted(closed.busy) == [name]
    assert _wait_exit(jobs[0].pid, timeout=1), "closure returned with the job alive"
    assert not terminal.has_session(name)
    assert strict.live_terminals() == []
    [notice] = _release_notices(home)
    assert notice["name"] == name and notice["operation"] == operation
    assert notice["reason"] == pty_close_notices.RELEASE_REASON


def test_release_closure_reports_a_sigkill_survivor_after_recording_its_notice(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """An unresolved closure fails with the survivor's identity; the notice was
    recorded before the cancel, so no retry or crash can lose it."""
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    (home / "machine_name").write_text("test-host")
    name = "ava-agent-987-shell-6004-unkillable"
    shell = _start_busy_session(terminal, home, name, _STUBBORN_JOB, pty_reaper)
    jobs = _started_jobs(shell, pty_reaper)
    assert jobs, "the stubborn job never started"
    deliver = OwnedProcess.send_signal

    def withheld_kill(identity: OwnedProcess, signum: int) -> bool:
        # Stand-in for an uninterruptible process: SIGKILL has no effect.
        return False if signum == signal.SIGKILL else deliver(identity, signum)

    monkeypatch.setattr(OwnedProcess, "send_signal", withheld_kill)
    with pytest.raises(StopIncompleteError) as caught:
        strict.close_release_terminals(
            str(uuid4()), WHEN, grace_s=0.3, kill_s=0.3, reason=pty_close_notices.RELEASE_REASON
        )
    assert caught.value.stage == "release-terminals"
    assert {(entry["pid"], entry["service"]) for entry in caught.value.survivors} >= {
        (jobs[0].pid, name)
    }
    assert not _has_exited(jobs[0])
    assert [notice["name"] for notice in _release_notices(home)] == [name]


def test_release_stop_closes_terminals_after_root_and_before_evidence(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """The release stop phase closes a live terminal instead of refusing it:
    work bound, root stop keeping terminals, closure, then root evidence."""
    from cli.commands import maintenance as maintenance_commands
    from cli.commands import root_driver
    from cli.release_transition import local

    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    name = "ava-agent-987-shell-6005-release"
    shell = _start_busy_session(terminal, home, name, _STUBBORN_JOB, pty_reaper)
    jobs = _started_jobs(shell, pty_reaper)
    assert jobs, "the stubborn job never started"
    events: list[str] = []

    def root_stop(*_args: object, **kwargs: object) -> None:
        assert kwargs["keep_terminals"] is True and shell.live(), "root stops first"
        events.append("root stop")

    def root_absent() -> None:
        assert not terminal.has_session(name), "terminals close before the evidence"
        events.append("root absent")

    for bound, value in (("_TERMINAL_WORK_S", 0.5), ("_TERMINAL_GRACE_S", 0.5)):
        monkeypatch.setattr(local, bound, value)
    monkeypatch.setattr(maintenance_commands, "stop", root_stop)
    monkeypatch.setattr(root_driver, "require_root_absent", root_absent)

    def drained(*_args: object) -> SimpleNamespace:
        return SimpleNamespace(maintenance=SimpleNamespace(phase="drained"))

    monkeypatch.setattr(maintenance, "require_operation", drained)
    transition = object.__new__(local.LocalTransition)
    transition.request = SimpleNamespace(id=uuid4(), created_at=WHEN)  # type: ignore[assignment]
    monkeypatch.setattr(transition, "preflight", lambda: None)

    transition.stop(SimpleNamespace(direction="candidate", launch=None))  # type: ignore[arg-type]
    assert events == ["root stop", "root absent"]
    assert _wait_exit(jobs[0].pid, timeout=1)


# ─── PITR boundary (decisions/2026-09-27-unit-join-pitr-closure-fleet-policy.md item 2) ──


def _no_op(_operation: object) -> None:
    return None


def test_pitr_closure_reports_a_sigkill_survivor_after_recording_its_notice(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """`close_release_terminals` is shared code: a PITR activation names its own
    reason, and an unresolved closure still fails with the survivor's identity
    after recording that notice."""
    from ops import pty_close_notices

    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    (home / "machine_name").write_text("test-host")
    name = "ava-agent-987-shell-6100-pitr-unkillable"
    shell = _start_busy_session(terminal, home, name, _STUBBORN_JOB, pty_reaper)
    jobs = _started_jobs(shell, pty_reaper)
    assert jobs, "the stubborn job never started"
    deliver = OwnedProcess.send_signal

    def withheld_kill(identity: OwnedProcess, signum: int) -> bool:
        return False if signum == signal.SIGKILL else deliver(identity, signum)

    monkeypatch.setattr(OwnedProcess, "send_signal", withheld_kill)
    with pytest.raises(StopIncompleteError) as caught:
        strict.close_release_terminals(
            str(uuid4()), WHEN, grace_s=0.3, kill_s=0.3, reason=pty_close_notices.PITR_REASON
        )
    assert caught.value.stage == "release-terminals"
    assert {(entry["pid"], entry["service"]) for entry in caught.value.survivors} >= {
        (jobs[0].pid, name)
    }
    assert not _has_exited(jobs[0])
    [notice] = _release_notices(home)
    assert notice["name"] == name and notice["reason"] == pty_close_notices.PITR_REASON


def test_pitr_stop_apps_closes_terminals_after_root_and_before_evidence(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """PITR's `stop_apps` closes a live terminal instead of refusing it, in the
    same order as a release's stop phase: work bound, root stop keeping
    terminals, closure (with a PITR-named notice for the busy owner), root
    evidence, then the post-closure terminal evidence check."""
    from cli.commands import maintenance as maintenance_commands
    from cli.commands import root_driver
    from cli.release_transition.pitr import transition as pitr_transition
    from ops import pty_close_notices
    from shared import maintenance

    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    (home / "machine_name").write_text("test-host")
    name = "ava-agent-987-shell-6101-pitr"
    shell = _start_busy_session(terminal, home, name, _STUBBORN_JOB, pty_reaper)
    jobs = _started_jobs(shell, pty_reaper)
    assert jobs, "the stubborn job never started"
    events: list[str] = []

    def root_stop(*_args: object, **kwargs: object) -> None:
        assert kwargs["keep_terminals"] is True and shell.live(), "root stops first"
        events.append("root stop")

    def root_absent() -> None:
        assert not terminal.has_session(name), "terminals close before the evidence"
        events.append("root absent")

    for bound, value in (("_TERMINAL_WORK_S", 0.5), ("_TERMINAL_GRACE_S", 0.5)):
        monkeypatch.setattr(pitr_transition, bound, value)
    monkeypatch.setattr(maintenance_commands, "stop", root_stop)
    monkeypatch.setattr(root_driver, "require_root_absent", root_absent)
    monkeypatch.setattr(pitr_transition, "require_inputs", _no_op)
    monkeypatch.setattr(pitr_transition.PitrTransition, "at", property(lambda _self: WHEN))

    def drained(*_args: object) -> SimpleNamespace:
        return SimpleNamespace(maintenance=SimpleNamespace(phase="drained"))

    monkeypatch.setattr(maintenance, "require_operation", drained)
    driver = object.__new__(pitr_transition.PitrTransition)
    driver.request = SimpleNamespace(id=uuid4())  # type: ignore[assignment]
    # A non-None data_stop skips the post-evidence PostgreSQL capture: this
    # test is scoped to terminal closure ordering, not data-plane custody.
    journal = SimpleNamespace(operation=SimpleNamespace(pitr=SimpleNamespace(data_stop="captured")))

    driver.stop_apps(journal)  # type: ignore[arg-type]
    assert events == ["root stop", "root absent"]
    assert _wait_exit(jobs[0].pid, timeout=1)
    [notice] = _release_notices(home)
    assert notice["name"] == name and notice["reason"] == pty_close_notices.PITR_REASON


def test_pitr_stop_apps_evidence_check_refuses_a_terminal_live_after_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`require_no_terminals` is the post-closure evidence check: a terminal
    that is somehow still alive right after `close_release_terminals` returns
    must refuse before PITR touches the data plane."""
    from cli.commands import maintenance as maintenance_commands
    from cli.commands import root_driver
    from cli.release_transition.pitr import transition as pitr_transition
    from shared import maintenance

    def drained(*_args: object) -> SimpleNamespace:
        return SimpleNamespace(maintenance=SimpleNamespace(phase="drained"))

    def no_busy(_timeout: float) -> list[str]:
        return []

    def root_stop(*_args: object, **_kwargs: object) -> None:
        return None

    def closed_nothing(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(shells={})

    def root_absent() -> None:
        return None

    def survivor() -> list[str]:
        return ["ava-agent-1-shell-2-survivor"]

    driver = object.__new__(pitr_transition.PitrTransition)
    driver.request = SimpleNamespace(id=uuid4())  # type: ignore[assignment]
    monkeypatch.setattr(pitr_transition.PitrTransition, "at", property(lambda _self: WHEN))
    monkeypatch.setattr(pitr_transition, "require_inputs", _no_op)
    monkeypatch.setattr(maintenance, "require_operation", drained)
    monkeypatch.setattr(strict, "await_terminal_work", no_busy)
    monkeypatch.setattr(maintenance_commands, "stop", root_stop)
    monkeypatch.setattr(strict, "close_release_terminals", closed_nothing)
    monkeypatch.setattr(root_driver, "require_root_absent", root_absent)
    monkeypatch.setattr(strict, "live_terminals", survivor)
    journal = SimpleNamespace(operation=SimpleNamespace(pitr=SimpleNamespace(data_stop="captured")))

    with pytest.raises(RuntimeError, match="will not kill or replay"):
        driver.stop_apps(journal)  # type: ignore[arg-type]
