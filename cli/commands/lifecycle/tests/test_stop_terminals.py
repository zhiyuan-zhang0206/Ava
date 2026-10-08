"""Terminal closure at a normal stop (#2045): the stop's side of the pty-sessions service.

The service holds every shell and runs the one terminal closure
(`base.sessions.pty.closure`, covered by `services/agent_runner/pty_sessions/tests/test_close_all.py`).
These tests lock what `ava stop` does around it, with real shells where the service
is the thing under test:

- `close_terminals` asks the service to close everything within a grace capped by
  the stop's deadline, writes one owner notice per closed busy session, and fails
  the stop when a shell survives; known job leftovers are diagnostic;
- a stop finding the service gone closes the leftovers its ledger names;
- `live_terminals` reads the service while it listens and the ledger when it does not;
- `--force` closes terminals with no grace and no notices.

A stop whose closure is stubbed (`_outcome`) stands in for processes the closure may
not signal (another user's, such as a root `sudo` on the pty), which no test can
create for real.
"""

from __future__ import annotations

import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil
import pytest

from base.deploy.maintenance import admission
from base.sessions.pty import client, closure
from base.sessions.pty.paths import SERVICE_UNIT, ledger_path
from cli.commands.lifecycle import _temporary_stop as command
from cli.commands.lifecycle import service_stop as strict
from cli.commands.lifecycle import stop as entry
from cli.commands.lifecycle._maintenance_stop_report import StopIncompleteError
from cli.commands.lifecycle.tests.stop_support import (
    Launcher,
    PtyServiceProcess,
    busy_session,
    closed_session,
    dependencies,
    identity_of,
    record_root_stops,
    stop_env,
    stub_closure,
)
from cli.commands.lifecycle.tests.stop_support import home as home
from cli.commands.lifecycle.tests.stop_support import launch as launch
from cli.commands.lifecycle.tests.stop_support import pty_service as pty_service
from cli.commands.lifecycle.tests.stop_support import written as written
from ops import pty_close_notices
from services.agent_runner.pty_sessions import ledger
from tests.path_scoped import pty_jobs as jobs
from tests.path_scoped import pty_shells as support
from tests.path_scoped.pty_reaper import PtyReaper
from tests.path_scoped.pty_shells import new, wait_for

# The idle test's own prompt, set by its home's `.bash_profile`.
_IDLE_PROMPT = "ava-idle-shell>"

# A process that ignores the closure's HUP and TERM, standing in as a shell or a job.
_STUBBORN_PROCESS = (
    "import signal,time; signal.signal(signal.SIGHUP,signal.SIG_IGN); "
    "signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
)

pytestmark = pytest.mark.usefixtures("written")


def _still_drained(_db: object, _bus: object, _timeout: float, **_kw: object) -> None:
    """The retry re-enters the held stop; the drain stand-in only opens a fresh hold."""


@pytest.mark.flaky
def test_stop_closes_busy_terminal_job_with_real_signals(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    written: list[pty_close_notices.ClosureNotice],
) -> None:
    """A regular interruptible foreground job closes via a normal stop, through the
    service, and its owner gets the closure notice."""
    dependencies(monkeypatch)
    stop_env(monkeypatch, home)
    name = "ava-agent-987-shell-2045-job"
    _, running = busy_session(home, name, jobs.TERM_OK, pty_reaper)
    assert running, "the synthetic job never started"

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=15) == 0
    assert not client.has_session(name)
    assert jobs.wait_exit(running[0].pid), "the job must be closed by the normal stop"
    assert [notice.name for notice in written] == [name]


@pytest.mark.flaky
def test_stop_kills_a_job_that_ignores_termination_after_its_grace(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    written: list[pty_close_notices.ClosureNotice],
) -> None:
    """A job that ignores TERM and HUP is SIGKILLed once the stop's grace ends: the
    stop completes well inside its deadline, and the owner still gets the closure
    notice for the work the kill cut short."""
    dependencies(monkeypatch)
    stop_env(monkeypatch, home)
    monkeypatch.setattr(strict, "_TERMINAL_STOP_GRACE_S", 1.0)
    name = "ava-agent-987-shell-2045-stubborn"
    _, running = busy_session(home, name, jobs.STUBBORN, pty_reaper, ready_line="stubborn-ready")
    assert running, "the stubborn job never started"

    started = time.monotonic()
    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=12) == 0
    assert time.monotonic() - started < 8, "the grace bounds the wait, not the stop deadline"
    assert jobs.wait_exit(running[0].pid, timeout=5), "the job outlived the stop"
    assert not client.has_session(name)
    assert len(written) == 1, "a killed busy session still leaves its notice"


def test_stop_records_nothing_for_idle_shell(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    written: list[pty_close_notices.ClosureNotice],
) -> None:
    """An idle shell (no jobs) closed by stop is silent: the TTL reaper's quiet-empty
    policy, never a blanket close notification (issue #2044 #3).

    The session has no initial command, so idle is the login shell at its first
    prompt. The test owns that prompt: the login shell reads this home's
    `.bash_profile` after the system's files, so the prompt is `_IDLE_PROMPT`
    whatever the host's or the developer's shell configuration prints.
    """
    dependencies(monkeypatch)
    stop_env(monkeypatch, home)
    (home / ".bash_profile").write_text(f"PS1='{_IDLE_PROMPT} '\n")
    name = "ava-agent-987-shell-2044-idle"
    assert new(name, home, {"AVA_HOME": str(home), "HOME": str(home)})
    pty_reaper.track_session(name)
    assert wait_for(lambda: support.screen(name).rstrip().endswith(_IDLE_PROMPT)), (
        "the login shell never printed its prompt"
    )

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=15) == 0
    assert not client.has_session(name)
    assert written == []


@pytest.mark.flaky
def test_stop_tolerates_naturally_exited_session(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
) -> None:
    """A session whose shell exited on its own before the stop is already gone from
    the service: it must not fail the stop."""
    dependencies(monkeypatch)
    stop_env(monkeypatch, home)
    name = "ava-agent-987-shell-2045-already-dead"
    shell, _ = busy_session(home, name, jobs.TERM_OK, pty_reaper)
    os.kill(shell.pid, signal.SIGKILL)
    assert wait_for(lambda: not client.has_session(name)), "the service kept the dead session"
    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=10) == 0


def test_the_stop_services_phase_keeps_the_pty_sessions_service_until_its_terminalsclosed_session(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
) -> None:
    """The service closes the terminals, so it is stopped only after them: the
    services phase preserves its unit, and a separate phase stops it once no
    session is left."""
    dependencies(monkeypatch)
    stop_env(monkeypatch, home)
    root_stops = record_root_stops(monkeypatch)
    phases: list[str] = []
    real_close = strict.close_terminals

    def close(*args: object, **kwargs: object) -> None:
        phases.append(f"terminals:{len(root_stops)}")
        real_close(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(command, "close_terminals", close)
    name = "ava-agent-987-shell-2044-order"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=15) == 0
    assert phases == ["terminals:1"], "terminals are closed after the services phase only"
    assert [SERVICE_UNIT in call["preserve"] for call in root_stops] == [True, False]
    assert client.list_sessions() == []


@pytest.mark.flaky
def test_incomplete_stop_still_records_the_sessions_itclosed_session(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    written: list[pty_close_notices.ClosureNotice],
) -> None:
    """One session closes; in another the shell itself outlives the stop. The stop is
    incomplete, yet the closed session's notice is recorded before it reports: the
    service no longer lists it, so a retry could never record it. The session whose
    shell still lives records nothing and is left to the retry, which records it:
    each notice once."""
    dependencies(monkeypatch)
    root_stops = record_root_stops(monkeypatch)
    stop_env(monkeypatch, home)
    closed = "ava-agent-987-shell-2051-closed"
    stuck = "ava-agent-987-shell-2052-stuck"
    shell = identity_of(launch(stuck, _STUBBORN_PROCESS))
    stub_closure(
        monkeypatch,
        closure.Outcome(
            closed=(closed_session(closed),),
            survivors=(closure.Survivor(stuck, shell, "terminal"),),
        ),
        closure.Outcome(closed=(closed_session(stuck),)),
    )

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=12) == 1
    assert admission.held(), "the hold must survive an incomplete stop"
    assert stuck in capsys.readouterr().err
    assert [notice.name for notice in written] == [closed], "the closed session's notice was lost"
    assert "survivors" not in written[0].as_dict(), "nothing of the closed session survived"
    assert [SERVICE_UNIT in call["preserve"] for call in root_stops] == [True], (
        "the service that holds the terminals is not stopped while the closure is incomplete"
    )

    monkeypatch.setattr(command, "pause_agents", _still_drained)
    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=12) == 0
    assert [notice.name for notice in written] == [closed, stuck]


def test_known_job_leftover_is_reported_without_failing_stop(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    written: list[pty_close_notices.ClosureNotice],
) -> None:
    """A known job can survive closure: report it and notify its owner, but release
    stop successfully once the shell and terminal are closed."""
    dependencies(monkeypatch)
    stop_env(monkeypatch, home)
    name = "ava-agent-987-shell-2044-stubborn"
    job = launch("private-job", _STUBBORN_PROCESS)
    survivor = closure.Survivor(name, identity_of(job), "job")
    stub_closure(
        monkeypatch,
        closure.Outcome(
            closed=(closed_session(name, ((job.pid, "python3"),)),), survivors=(survivor,)
        ),
        closure.Outcome(),
    )

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=12) == 0
    assert admission.held(), "a successful stop still fences the stopped unit"
    assert psutil.pid_exists(job.pid)
    err = capsys.readouterr().err
    assert name in err
    assert f"pid={job.pid}" in err
    assert "inspect the process" in err
    assert [notice.name for notice in written] == [name]
    assert written[0].survivors == ((job.pid, "python3"),)

    monkeypatch.setattr(command, "pause_agents", _still_drained)
    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=12) == 0
    assert len(written) == 1, "the retry recorded the closed session again"


def test_a_survivor_that_died_before_the_report_does_not_fail_the_stop(
    home: Path, monkeypatch: pytest.MonkeyPatch, launch: Launcher
) -> None:
    """The report names only survivors that are still their captured process: one that
    exited after the closure's verdict is gone, and the stop completes."""
    dependencies(monkeypatch)
    stop_env(monkeypatch, home)
    proc = launch("private-gone", _STUBBORN_PROCESS)
    gone = identity_of(proc)
    proc.kill()
    proc.wait(timeout=5)
    stub_closure(
        monkeypatch, closure.Outcome(survivors=(closure.Survivor("gone", gone, "terminal"),))
    )
    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=12) == 0


@pytest.mark.parametrize(
    ("remaining_s", "expected_grace"),
    [(3.0, 3.0), (100.0, strict._TERMINAL_STOP_GRACE_S), (-1.0, 0.0)],
)
def test_the_closure_grace_is_the_stops_remaining_time_up_to_its_ceiling(
    remaining_s: float,
    expected_grace: float,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grace never exceeds the stop's deadline or the ceiling; the SIGKILL leg runs
    with its own bound even when the deadline is already spent."""
    monkeypatch.setenv("AVA_HOME", str(home))
    asked = stub_closure(monkeypatch, closure.Outcome())

    def settled(_until: float, _stage: str) -> None:
        """Stand-in for the closure evidence: nothing is left."""

    monkeypatch.setattr(strict, "_await_no_terminals", settled)
    strict.close_terminals(
        time.monotonic() + remaining_s, "stop-test", datetime.now(UTC), direct_db=False
    )
    ((grace, kill),) = asked
    assert expected_grace - 0.5 <= grace <= expected_grace
    assert kill == strict._TERMINAL_KILL_WAIT_S


def test_a_terminal_left_after_the_closure_fails_the_stop_in_its_own_words(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal still present once the closure ended every session it captured
    fails the stop, naming it, and without maintenance's claim that nothing will be
    killed: this stop has just killed its sessions."""
    name = "ava-agent-987-shell-2053-lingering"
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setattr(strict, "live_terminals", lambda: [name])

    with pytest.raises(StopIncompleteError) as excinfo:
        strict._await_no_terminals(time.monotonic(), "terminals")
    message = str(excinfo.value)
    assert name in message
    assert "will not kill" not in message
    assert excinfo.value.stage == "terminals"


def test_a_terminal_that_clears_within_the_stop_deadline_does_not_fail_the_stop(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closure evidence waits until the stop's deadline, and at least the SIGKILL
    leg's bound: a session still tearing down past that bound but gone from the
    service before the deadline is a closed terminal, not a failed stop."""
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setattr(strict, "_TERMINAL_KILL_WAIT_S", 0.2)
    stub_closure(monkeypatch, closure.Outcome())
    cleared_at = time.monotonic() + 0.6
    name = "ava-agent-987-shell-2053-tearing-down"

    def tearing_down() -> list[str]:
        return [] if time.monotonic() >= cleared_at else [name]

    monkeypatch.setattr(strict, "live_terminals", tearing_down)
    strict.close_terminals(time.monotonic() + 10, "stop-test", datetime.now(UTC), direct_db=False)
    assert time.monotonic() >= cleared_at


def test_live_terminals_reads_the_service_while_it_listens(
    home: Path, pty_service: PtyServiceProcess
) -> None:
    """With the service listening its listing is the truth, and any session in it
    refuses maintenance."""
    assert strict.live_terminals() == []
    strict.require_no_terminals()
    name = "ava-agent-987-shell-2053-listed"
    assert new(name, home, {"HOME": str(home)})

    assert strict.live_terminals() == [name]
    with pytest.raises(RuntimeError, match=name):
        strict.require_no_terminals()


def test_live_terminals_without_the_service_is_what_its_ledger_still_runs(
    home: Path, launch: Launcher
) -> None:
    """A service that is not listening has no sessions; a crash's leftover is a process
    its ledger names that is still its recorded process."""
    assert strict.live_terminals() == []
    name = "ava-agent-987-shell-2053-leftover"
    proc = launch(name, _STUBBORN_PROCESS)
    ledger.write(ledger_path(), [closure.Target(name, identity_of(proc))])
    assert strict.live_terminals() == [name]
    proc.kill()
    proc.wait(timeout=5)
    assert strict.live_terminals() == []


@pytest.mark.parametrize("ignores_hangup", [False, True])
def test_a_stop_without_the_service_closes_the_leftovers_its_ledger_names(
    ignores_hangup: bool, home: Path, launch: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No service listens, so there is no one to ask: the stop closes from the ledger
    the dead service left, the leftover hung up first and SIGKILLed when it ignores
    the hangup, and the ledger is then empty."""
    monkeypatch.setattr(ledger, "SWEEP_HANGUP_WAIT_S", 0.3)
    name = "ava-agent-987-shell-2054-orphaned"
    code = (
        _STUBBORN_PROCESS
        if ignores_hangup
        else "import time; print('ready',flush=True); time.sleep(60)"
    )
    proc = launch(name, code)
    ledger.write(ledger_path(), [closure.Target(name, identity_of(proc))])
    assert strict.live_terminals() == [name]

    strict.close_terminals(time.monotonic() + 10, "stop-test", datetime.now(UTC), direct_db=False)
    assert proc.wait(timeout=10) == (-signal.SIGKILL if ignores_hangup else -signal.SIGHUP)
    assert strict.live_terminals() == []
    assert ledger.read(ledger_path()) == []


@pytest.mark.flaky
def test_force_close_closes_every_session_at_once_without_notices(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    written: list[pty_close_notices.ClosureNotice],
) -> None:
    """`--force` closes terminals with no grace: a job that ignores TERM and HUP is
    SIGKILLed at once, and no owner notice is written."""
    monkeypatch.setenv("AVA_HOME", str(home))
    name = "ava-agent-987-shell-2055-force"
    _, running = busy_session(home, name, jobs.STUBBORN, pty_reaper, ready_line="stubborn-ready")
    assert running, "the stubborn job never started"

    started = time.monotonic()
    strict.force_close_terminals()
    assert time.monotonic() - started < 8
    assert not client.has_session(name)
    assert jobs.wait_exit(running[0].pid, timeout=5), "the job outlived the forced close"
    assert written == []


def test_force_close_reports_known_job_without_failing(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AVA_HOME", str(home))
    name = "ava-agent-987-shell-2055-denied"
    job = launch("private-job", _STUBBORN_PROCESS)
    asked = stub_closure(
        monkeypatch,
        closure.Outcome(survivors=(closure.Survivor(name, identity_of(job), "job"),)),
    )
    strict.force_close_terminals()
    assert name in capsys.readouterr().err
    assert asked == [(0.0, strict._TERMINAL_KILL_WAIT_S)]


def test_force_close_still_fails_when_known_shell_survives(
    home: Path, launch: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AVA_HOME", str(home))
    name = "private-surviving-shell"
    shell = launch(name, _STUBBORN_PROCESS)
    stub_closure(
        monkeypatch,
        closure.Outcome(survivors=(closure.Survivor(name, identity_of(shell), "terminal"),)),
    )
    with pytest.raises(RuntimeError, match=name):
        strict.force_close_terminals()


@pytest.mark.parametrize("force", [False, True])
def test_operational_terminal_close_failure_is_not_best_effort_success(
    monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    def failed_close(_grace_s: float, _kill_s: float) -> closure.Outcome:
        raise client.ServiceError(1, "known group signal denied")

    monkeypatch.setattr(strict, "_close_via_service", failed_close)
    with pytest.raises(client.ServiceError, match="known group signal denied"):
        if force:
            strict.force_close_terminals()
        else:
            strict.close_terminals(
                time.monotonic() + 1, "stop-test", datetime.now(UTC), direct_db=False
            )
