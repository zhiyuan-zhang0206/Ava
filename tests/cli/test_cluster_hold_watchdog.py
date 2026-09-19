"""cli.commands._cluster_hold_watchdog — the completion attempt machine (task #3887).

Pins the contract the OS scheduler depends on: the ladder per phase matches
the official stop/start/resume recipe, the attempt budget bounds the whole
mechanism to one shot per generation, a hold released mid-attempt records the
"rescued within the window" abort (never a completion), and every outcome
reaches both the attempt log and the scheduler-captured stderr.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from cli.commands import _cluster_hold_watchdog as cw
from shared import hold_watchdog as hw

_AT = datetime(2026, 9, 17, 22, 27, 46, tzinfo=UTC)


def _eligible(phase: str = "stopped") -> hw.HoldWatchdogVerdict:
    return hw.HoldWatchdogVerdict(
        kind=hw.VerdictKind.ELIGIBLE,
        code="due",
        detail="hold has no live owner and nothing executes under it",
        holder="wsl:pid1",
        acquired_at=_AT,
        phase=phase,
        age_s=3600.0,
        due_in_s=-100.0,
    )


def _back_off(code: str, detail: str = "deferred") -> hw.HoldWatchdogVerdict:
    return hw.HoldWatchdogVerdict(
        kind=hw.VerdictKind.BACK_OFF,
        code=code,
        detail=detail,
        holder="wsl:pid1",
        acquired_at=_AT,
        phase="stopped",
    )


def _no_hold() -> hw.HoldWatchdogVerdict:
    return hw.HoldWatchdogVerdict(kind=hw.VerdictKind.NO_HOLD, code="no-hold")


@pytest.fixture(autouse=True)
def _clean_attempt_state() -> Iterator[None]:
    from shared import host_deploy_state as hds

    paths = (
        hw.attempt_path(),
        hw.attempt_lock_path(),
        hds.pending_stranded_recovery_note_path(),
    )
    for path in paths:
        path.unlink(missing_ok=True)
    yield
    for path in paths:
        path.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _ready_completion_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the pre-attempt gate's two seams to a resolvable pure runner.

    The gate (task #4080) reads this unit's capability set and, on a pure
    runner, the bootstrap values the start leg will boot with — a bare test
    environment has neither, so without the pins every eligible test would
    defer. Pinning the seams rather than the gate keeps the real gate logic
    exercised on every run; the gate tests below re-patch one seam each.
    """
    monkeypatch.setattr("cli.commands._repo._roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(
        "shared.bootstrap.resolve_bootstrap_values",
        lambda: {"AVA_GATEWAY_OTLP_ENDPOINT": "http://10.0.0.5:4318"},
    )


@pytest.fixture
def legs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the three official legs with recorders.

    The command reaches them through the `cli.commands._hold_recover` module,
    so patching the module's attributes is exactly how production resolves
    them.
    """
    from cli.commands import _hold_recover

    calls: list[str] = []

    def _start(_holder: str, _at: datetime) -> None:
        calls.append("start")

    def _resume(_holder: str, _at: datetime) -> None:
        calls.append("resume")

    monkeypatch.setattr(_hold_recover, "_complete_stop", lambda: calls.append("stop"))
    monkeypatch.setattr(_hold_recover, "_start_leg", _start)
    monkeypatch.setattr(_hold_recover, "_resume_leg", _resume)
    return calls


@pytest.fixture
def db_notes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Keep the best-effort DB mirror out of the test environment and record it."""
    from shared import host_deploy_state

    notes: list[str] = []
    monkeypatch.setattr(host_deploy_state, "finish_stranded_recovery", notes.append)
    return notes


def _verdicts(monkeypatch: pytest.MonkeyPatch, *sequence: hw.HoldWatchdogVerdict) -> None:
    """Pin evaluate() to a call sequence (pre-reserve, recheck, post-failure reads)."""
    remaining = list(sequence)

    def _next(**_kwargs: object) -> hw.HoldWatchdogVerdict:
        return remaining.pop(0) if remaining else sequence[-1]

    monkeypatch.setattr(hw, "evaluate", _next)


# --- quiet readings ---------------------------------------------------------


def test_no_hold_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _verdicts(monkeypatch, _no_hold())
    assert cw.cmd_hold_watchdog() == 0
    assert capsys.readouterr().err == ""


def test_a_deferring_verdict_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _verdicts(monkeypatch, _back_off("young", "hold is 60s old"))
    assert cw.cmd_hold_watchdog() == 0
    err = capsys.readouterr().err
    assert "standing down" in err and "hold is 60s old" in err


# --- the ladder --------------------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "want"),
    [
        ("stopping", ["stop", "start", "resume"]),
        ("stopped", ["start", "resume"]),
        ("starting", ["start", "resume"]),
    ],
)
def test_the_ladder_follows_the_official_recipe(
    phase: str,
    want: list[str],
    legs: list[str],
    db_notes: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _verdicts(monkeypatch, _eligible(phase))
    assert cw.cmd_hold_watchdog() == 0
    assert legs == want
    err = capsys.readouterr().err
    assert "ORPHAN HOLD" in err
    assert "expired-complete" in err


def test_a_completed_attempt_records_the_expired_complete_note(
    legs: list[str], db_notes: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _verdicts(monkeypatch, _eligible())
    assert cw.cmd_hold_watchdog() == 0
    state = hw.read_attempt()
    assert state is not None
    assert state.note is not None and state.note.startswith("expired-complete")
    assert state.attempts == 1
    assert db_notes and "hold-watchdog: expired-complete" in db_notes[0]


def test_a_failed_step_spends_the_attempt_and_reports(
    legs: list[str],
    db_notes: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.commands import _hold_recover

    def _boom(_h: str, _t: datetime) -> None:
        raise RuntimeError("start leg exited 1")

    monkeypatch.setattr(_hold_recover, "_start_leg", _boom)
    _verdicts(monkeypatch, _eligible())
    assert cw.cmd_hold_watchdog() == 1
    state = hw.read_attempt()
    assert state is not None and state.note is not None
    assert state.note.startswith("failed at start")
    err = capsys.readouterr().err
    assert "failed at start" in err


def test_a_mid_ladder_release_records_the_rescued_wording(
    legs: list[str],
    db_notes: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A release that sweeps the hold away mid-ladder is a rescue, not a
    failure: the leg's refusal must record the #6294 abort wording."""
    from cli.commands import _hold_recover

    def _boom(_h: str, _t: datetime) -> None:
        raise RuntimeError("this unit is not held by the supplied maintenance generation")

    monkeypatch.setattr(_hold_recover, "_start_leg", _boom)
    _verdicts(monkeypatch, _eligible(), _eligible(), _no_hold())
    assert cw.cmd_hold_watchdog() == 1
    state = hw.read_attempt()
    assert state is not None and state.note is not None
    assert state.note.startswith("aborted (rescued within the window)")
    err = capsys.readouterr().err
    assert "rescued within the window" in err
    assert db_notes and "rescued within the window" in db_notes[0]


def test_the_attempt_log_carries_the_narrative(
    legs: list[str], db_notes: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import shared.paths

    _verdicts(monkeypatch, _eligible("stopping"))
    assert cw.cmd_hold_watchdog() == 0
    logs = sorted((shared.paths.ava_home() / "logs").glob("hold-watchdog-*.log"))
    assert logs, "no attempt log written"
    text = logs[-1].read_text()
    assert "attempt #1" in text
    assert "ladder step: stop" in text
    assert "outcome: expired-complete" in text


# --- the budget --------------------------------------------------------------


def test_the_second_run_of_a_generation_spends_nothing(
    legs: list[str],
    db_notes: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One attempt per generation, whatever the scheduler's cadence: the
    second evaluation reads the same orphan and must stand down."""
    _verdicts(monkeypatch, _eligible())
    assert cw.cmd_hold_watchdog() == 0
    assert legs == ["start", "resume"]
    capsys.readouterr()
    assert cw.cmd_hold_watchdog() == 0
    assert legs == ["start", "resume"], "a second ladder ran on a spent budget"
    assert "budget is spent" in capsys.readouterr().err


# --- the semantics split -----------------------------------------------------


def test_a_released_hold_aborts_with_the_rescued_wording(
    legs: list[str],
    db_notes: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Constraint 1 of #6294: an external release inside the window is a
    rescue, never a completion - and the wording must say so."""
    _verdicts(monkeypatch, _eligible(), _no_hold())
    assert cw.cmd_hold_watchdog() == 0
    assert legs == []
    state = hw.read_attempt()
    assert state is not None and state.note is not None
    assert state.note.startswith("aborted (rescued within the window)")
    err = capsys.readouterr().err
    assert "rescued within the window" in err


def test_a_replaced_generation_aborts_with_the_rescued_wording(
    legs: list[str], db_notes: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    replaced = hw.HoldWatchdogVerdict(
        kind=hw.VerdictKind.ELIGIBLE,
        code="due",
        detail="a newer hold generation",
        holder="wsl:pid2",
        acquired_at=_AT,
        phase="stopped",
    )
    _verdicts(monkeypatch, _eligible(), replaced)
    assert cw.cmd_hold_watchdog() == 0
    assert legs == []
    state = hw.read_attempt()
    assert state is not None and state.note is not None
    assert state.note.startswith("aborted (rescued within the window)")


def test_a_gate_appearing_mid_flight_aborts_before_the_ladder(
    legs: list[str], db_notes: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same generation but a new blocker (e.g. a boot start appeared) is
    not a rescue; the note says the attempt was spent before acting."""
    _verdicts(monkeypatch, _eligible(), _back_off("lifecycle-busy", "a start is running"))
    assert cw.cmd_hold_watchdog() == 0
    assert legs == []
    state = hw.read_attempt()
    assert state is not None and state.note is not None
    assert state.note.startswith("aborted before the ladder: lifecycle-busy")


# --- registration entries ----------------------------------------------------


def test_register_reports_a_registration_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom() -> None:
        raise RuntimeError("launchd said no")

    monkeypatch.setattr("shared.os_hold_watchdog.register_hold_watchdog", _boom)
    assert cw.cmd_hold_watchdog_register() == 1
    assert "launchd said no" in capsys.readouterr().err


def test_register_and_unregister_call_through(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "shared.os_hold_watchdog.register_hold_watchdog", lambda: calls.append("register")
    )
    monkeypatch.setattr(
        "shared.os_hold_watchdog.unregister_hold_watchdog", lambda: calls.append("unregister")
    )
    assert cw.cmd_hold_watchdog_register() == 0
    assert cw.cmd_hold_watchdog_unregister() == 0
    assert calls == ["register", "unregister"]


def test_an_unexpected_failure_is_reported_not_traced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _explode(**_kwargs: object) -> hw.HoldWatchdogVerdict:
        raise RuntimeError("boom")

    monkeypatch.setattr(hw, "evaluate", _explode)
    assert cw.cmd_hold_watchdog() == 1
    assert "unexpected failure" in capsys.readouterr().err


# --- the completion gate (task #4080) ----------------------------------------


def test_a_runner_awaiting_the_published_endpoint_defers_without_spending(
    legs: list[str],
    db_notes: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The 2026-09-19 migration shape: the gateway has not published the relay
    ingress, the start leg cannot build it — the attempt must wait, not burn."""
    monkeypatch.setattr("shared.bootstrap.resolve_bootstrap_values", dict)
    _verdicts(monkeypatch, _eligible())
    assert cw.cmd_hold_watchdog() == 0
    err = capsys.readouterr().err
    assert "deferred" in err
    assert "not published" in err
    assert "spends no attempt" in err
    assert "ORPHAN HOLD" not in err
    assert legs == []
    assert hw.read_attempt() is None, "the gate deferred but the attempt was spent"
    assert db_notes == []


def test_a_runner_whose_config_cannot_resolve_defers(
    legs: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from shared.bootstrap import BootstrapFetchError

    def _boom() -> dict[str, str]:
        raise BootstrapFetchError(
            "could not fetch cluster config from the gateway at http://10.0.0.1:8000 "
            "(ConnectError).\n    trailing advice the gate must not echo"
        )

    monkeypatch.setattr("shared.bootstrap.resolve_bootstrap_values", _boom)
    _verdicts(monkeypatch, _eligible())
    assert cw.cmd_hold_watchdog() == 0
    err = capsys.readouterr().err
    assert "deferred" in err
    assert "could not fetch cluster config from the gateway" in err
    assert "trailing advice" not in err  # one line, not the paragraph
    assert hw.read_attempt() is None


def test_an_invalid_published_endpoint_defers(
    legs: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "shared.bootstrap.resolve_bootstrap_values",
        lambda: {"AVA_GATEWAY_OTLP_ENDPOINT": "http://127.0.0.1:4318"},
    )
    _verdicts(monkeypatch, _eligible())
    assert cw.cmd_hold_watchdog() == 0
    assert "non-loopback" in capsys.readouterr().err
    assert hw.read_attempt() is None


def test_the_gate_has_no_question_for_a_gateway_capability_set(
    legs: list[str], db_notes: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a pure agent-runner converges the gateway relay; a unit that also
    serves the gateway completes as before."""
    monkeypatch.setattr(
        "cli.commands._repo._roles_or_none", lambda: frozenset({"gateway", "agent-runner"})
    )
    _verdicts(monkeypatch, _eligible())
    assert cw.cmd_hold_watchdog() == 0
    assert legs == ["start", "resume"]


def test_an_unresolvable_capability_set_defers(
    legs: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("cli.commands._repo._roles_or_none", lambda: None)
    _verdicts(monkeypatch, _eligible())
    assert cw.cmd_hold_watchdog() == 0
    assert "capability set cannot be resolved" in capsys.readouterr().err
    assert hw.read_attempt() is None


# --- the fleet-record queue (task #4080) -------------------------------------


def test_an_unreachable_fleet_record_queues_the_outcome(
    legs: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A settings-lite run cannot dial a pure runner's database: the write is
    carried in the local queue instead — plainly, without the raw guard text."""
    from shared import host_deploy_state
    from shared.db_connections import UnanchoredHomeError

    def _boom(_note: str) -> None:
        raise UnanchoredHomeError(
            "refusing to open a DB connection: AVA_DB_URL is the never-dialed placeholder."
        )

    monkeypatch.setattr(host_deploy_state, "finish_stranded_recovery", _boom)
    _verdicts(monkeypatch, _eligible())
    assert cw.cmd_hold_watchdog() == 0
    err = capsys.readouterr().err
    assert "queued for backfill" in err
    assert "UnanchoredHomeError" not in err
    queued = host_deploy_state.pending_stranded_recovery_note()
    assert queued is not None
    assert queued.startswith("hold-watchdog: expired-complete")


def test_a_queued_note_is_backfilled_on_a_later_run(
    db_notes: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from shared import host_deploy_state

    host_deploy_state.queue_stranded_recovery_note("hold-watchdog: old outcome")
    _verdicts(monkeypatch, _no_hold())
    assert cw.cmd_hold_watchdog() == 0
    assert db_notes == ["hold-watchdog: old outcome"]
    assert host_deploy_state.pending_stranded_recovery_note() is None
    assert "backfilled" in capsys.readouterr().err
