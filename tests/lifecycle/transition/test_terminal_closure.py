"""Terminal closure at a release or a PITR activation: real PTYs, real signals.

A release runs `ava stop`'s terminal closure (tests/cli/test_stop_terminals.py)
with its own bounds (decisions/2026-09-27-fleet-release-and-cutover-policies.md
item 2): terminals get a bounded completed-work wait first, then the hang-up
and the SIGKILL leg — none survives, and each busy owner gets the `ava stop`
closure notice naming the release. A PITR activation closes them the same way
(decisions/2026-09-27-unit-join-pitr-closure-fleet-policy.md item 2).
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cli.commands import service_stop as strict
from cli.commands._maintenance_stop_report import StopIncompleteError
from ops import pty_close_notices
from shared.deploy.maintenance import admission as maintenance
from shared.platform import IS_WINDOWS
from shared.session_backend import PtySessionBackend
from shared.sessions.pty import session_tree
from tests.agent.test_maintenance import WHEN
from tests.cli.conftest import PtyReaper
from tests.cli.conftest import pty_reaper as pty_reaper
from tests.cli.test_pause_stop import home as home
from tests.cli.test_stop_terminals import (
    _STUBBORN_JOB,
    _TERM_OK_JOB,
    _denied_but_the_shell,
    _double_forked_job,
    _has_exited,
    _notice_files,
    _notices,
    _session_orphan,
    _start_busy_session,
    _started_jobs,
    _stop_env,
    _unkillable,
    _wait_exit,
)

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="pty sessions are POSIX-only")


# ─── release boundary (FC-6) ─────────────────────────────────────────────────


def _finishing_job(marker: Path) -> str:
    return (
        "import pathlib,time\n"
        "print('finishing-ready', flush=True)\n"
        "time.sleep(2.0)\n"
        f"pathlib.Path({str(marker)!r}).write_text('done')\n"
    )


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
    [notice] = _notices(home)
    assert notice["name"] == name and notice["operation"] == operation
    assert notice["reason"] == pty_close_notices.RELEASE_REASON


def test_release_closure_kills_a_double_forked_job_outside_the_shell_tree(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """A worker that double-forked out of the shell's tree and ignores the
    cancel is still the session's work: the session is busy, its owner gets the
    notice, and the worker dies with the session (`session_tree`)."""
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    (home / "machine_name").write_text("test-host")
    name = "ava-agent-987-shell-6006-orphan"
    pidfile = home / "orphan.pid"
    job = _double_forked_job(pidfile, ignore=("SIGTERM", "SIGHUP"))
    shell = _start_busy_session(terminal, home, name, job, pty_reaper)
    orphan = _session_orphan(pidfile, shell, pty_reaper)

    closed = strict.close_release_terminals(
        str(uuid4()), WHEN, grace_s=0.5, kill_s=10, reason=pty_close_notices.RELEASE_REASON
    )
    assert sorted(closed.busy) == [name], "the worker is the session's running work"
    assert _wait_exit(orphan.pid, timeout=1), "closure returned with the worker alive"
    assert strict.live_terminals() == []
    [notice] = _notices(home)
    assert notice["name"] == name


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
    _unkillable(monkeypatch)
    with pytest.raises(StopIncompleteError) as caught:
        strict.close_release_terminals(
            str(uuid4()), WHEN, grace_s=0.3, kill_s=0.3, reason=pty_close_notices.RELEASE_REASON
        )
    assert caught.value.stage == "release-terminals"
    assert {(entry["pid"], entry["service"]) for entry in caught.value.survivors} >= {
        (jobs[0].pid, name)
    }
    assert not _has_exited(jobs[0])
    assert [notice["name"] for notice in _notices(home)] == [name]


class ExecutorLostError(BaseException):
    """The release executor dies mid-closure: no compensation runs."""


def test_an_executor_dying_after_its_first_signal_still_leaves_the_notice(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """The notice is the closure's intent, recorded before any signal: an
    executor that dies after hanging up — before the kill and its post-kill
    record — still leaves the owner's notice behind."""
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    (home / "machine_name").write_text("test-host")
    name = "ava-agent-987-shell-6008-executor-lost"
    shell = _start_busy_session(terminal, home, name, _STUBBORN_JOB, pty_reaper)
    assert _started_jobs(shell, pty_reaper), "the stubborn job never started"
    operation = str(uuid4())

    def dies_in_the_grace(*_args: object) -> bool:
        raise ExecutorLostError

    monkeypatch.setattr(strict, "_await_members", dies_in_the_grace)
    with pytest.raises(ExecutorLostError):
        strict.close_release_terminals(
            operation, WHEN, grace_s=0.5, kill_s=10, reason=pty_close_notices.RELEASE_REASON
        )
    [notice] = _notices(home)
    assert notice["name"] == name and notice["operation"] == operation


def test_a_busy_shell_that_outlives_its_sigkill_still_gets_its_notice(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """A shell the closure may not signal (another user's) is never verified
    closed, so the post-kill record skips its session and the closure fails;
    only the intent recorded before any signal tells its owner."""
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    (home / "machine_name").write_text("test-host")
    name = "ava-agent-987-shell-6009-shell-survives"
    shell = _start_busy_session(terminal, home, name, _STUBBORN_JOB, pty_reaper)
    assert _started_jobs(shell, pty_reaper), "the stubborn job never started"

    def ignored_hang_up(_terminals: object) -> None:
        return None

    monkeypatch.setattr(strict, "_hang_up", ignored_hang_up)
    _unkillable(monkeypatch)
    with pytest.raises(StopIncompleteError):
        strict.close_release_terminals(
            str(uuid4()), WHEN, grace_s=0.3, kill_s=0.3, reason=pty_close_notices.RELEASE_REASON
        )
    assert shell.live(), "precondition: the shell outlived the closure"
    [notice] = _notices(home)
    assert notice["name"] == name and notice["reason"] == pty_close_notices.RELEASE_REASON


def test_release_closure_names_what_outlived_its_kill_in_the_one_notice(
    home: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """The release records its intent before the cancel; once the kill has
    ended the shell, the closure records the session again as `ava stop`
    would, naming the job that outlived its SIGKILL. Both land on the shell's
    one dedup key: the owner has one notice, and it names the survivor."""
    terminal = PtySessionBackend()
    _stop_env(monkeypatch, home, terminal)
    (home / "machine_name").write_text("test-host")
    name = "ava-agent-987-shell-6007-survivor"
    shell = _start_busy_session(terminal, home, name, _STUBBORN_JOB, pty_reaper)
    jobs = _started_jobs(shell, pty_reaper)
    assert jobs, "the stubborn job never started"
    monkeypatch.setattr(session_tree, "kill_session_tree", _denied_but_the_shell)

    with pytest.raises(StopIncompleteError) as caught:
        strict.close_release_terminals(
            str(uuid4()), WHEN, grace_s=0.3, kill_s=3, reason=pty_close_notices.RELEASE_REASON
        )
    assert caught.value.stage == "release-terminals"
    assert not _has_exited(jobs[0])
    [notice] = _notices(home)
    assert notice["name"] == name and notice["reason"] == pty_close_notices.RELEASE_REASON
    assert notice["survivors"] == [{"pid": jobs[0].pid, "name": jobs[0].name()}]


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

    monkeypatch.setattr(maintenance_commands, "stop", root_stop)
    monkeypatch.setattr(root_driver, "require_root_absent", root_absent)

    def drained(*_args: object) -> SimpleNamespace:
        return SimpleNamespace(maintenance=SimpleNamespace(phase="drained"))

    monkeypatch.setattr(maintenance, "require_operation", drained)
    from cli.release_fleet.policy import FleetPolicy

    transition = object.__new__(local.LocalTransition)
    # The captured policy's closure bounds: the work wait and the cancel grace.
    policy = FleetPolicy(close_s=1, cancel_grace_s=1)
    transition.request = SimpleNamespace(id=uuid4(), policy=policy)  # type: ignore[assignment]
    monkeypatch.setattr(transition, "preflight", lambda: None)

    operation = SimpleNamespace(direction="candidate", launch=None, maintenance_at=WHEN)
    transition.stop(operation)  # type: ignore[arg-type]
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
    _unkillable(monkeypatch)
    with pytest.raises(StopIncompleteError) as caught:
        strict.close_release_terminals(
            str(uuid4()), WHEN, grace_s=0.3, kill_s=0.3, reason=pty_close_notices.PITR_REASON
        )
    assert caught.value.stage == "release-terminals"
    assert {(entry["pid"], entry["service"]) for entry in caught.value.survivors} >= {
        (jobs[0].pid, name)
    }
    assert not _has_exited(jobs[0])
    [notice] = _notices(home)
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
    from shared.deploy.maintenance import admission as maintenance

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
    [notice] = _notices(home)
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
    from shared.deploy.maintenance import admission as maintenance

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
