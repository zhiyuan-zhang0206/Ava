"""The stranded-hold completion entry (task #3142) — `python -m cli.commands._hold_recover`.

The entry is the automation half of the operator recipe: it re-verifies the
hold's exact generation, phase and stranded verdict, then runs the same
stop / start / resume legs an operator runs by hand, recording the outcome in
the host's durable record either way. These tests lock the sequence per phase,
the refusal cases (a hold that moved on is not ours to complete) and the exit
codes the attempt's outcome is read from.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

import pytest

from cli.commands import _hold_recover as hr
from ops.controllers import stranded_pause as sp
from shared import pause_owner
from shared.config import settings
from shared.maintenance_state import MaintenanceHold
from shared.updater_handoff import UpdaterHandoffSnapshot

_AT = datetime(2026, 9, 12, 3, 0, tzinfo=UTC)
_HOLDER = "gateway-host:pid4242"

_VerdictKind = Literal["stranded", "clear", "unknown"]


def _snapshot(phase: str = "stopping", *, holder: str = _HOLDER) -> object:
    return pause_owner.PauseOwnerSnapshot(
        status="paused",
        holder=holder,
        acquired_at=_AT,
        maintenance=MaintenanceHold(phase=phase),  # type: ignore[arg-type]
    )


def _verdict_reader(
    kind: _VerdictKind,
) -> Callable[[UpdaterHandoffSnapshot | None], sp.StrandedHoldVerdict]:
    """A stand-in for `stranded_hold_verdict` that ignores its handoff argument."""

    def _read(_handoff: UpdaterHandoffSnapshot | None = None) -> sp.StrandedHoldVerdict:
        return sp.StrandedHoldVerdict(kind=kind, detail="updater exited rc=1", paused_for=700.0)

    return _read


class _Legs:
    """Recorded leg calls; every leg succeeds unless a test says otherwise."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.notes: list[str] = []
        self.stop_rc = 0
        self.start_rc = 0

    def stop(self, *_args: object, **_kwargs: object) -> int:
        self.calls.append("stop")
        return self.stop_rc

    def start(self, *_args: object, **_kwargs: object) -> int:
        self.calls.append("start")
        return self.start_rc

    def resume(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("resume")

    def finish(self, note: str) -> None:
        self.notes.append(note)


@pytest.fixture
def legs(monkeypatch: pytest.MonkeyPatch) -> _Legs:
    """Fake the three legs and the outcome write; record the call order."""
    import shared.host_deploy_state as hds
    from cli.commands import _maintenance as maint
    from cli.commands import stop

    rec = _Legs()
    monkeypatch.setattr(stop, "_do_stop", rec.stop)
    monkeypatch.setattr(maint, "_start", rec.start)
    monkeypatch.setattr(maint, "_resume", rec.resume)
    monkeypatch.setattr(hds, "finish_stranded_recovery", rec.finish)
    return rec


@pytest.fixture
def licensed(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """License the attempt: a stranded verdict over a hold at `phase`."""

    def _plant(phase: str, *, holder: str = _HOLDER) -> None:
        from shared import maintenance

        monkeypatch.setattr(maintenance, "snapshot", lambda: _snapshot(phase, holder=holder))
        monkeypatch.setattr(sp, "stranded_hold_verdict", _verdict_reader("stranded"))

    return _plant


def test_completes_a_stopping_hold_through_stop_start_resume(
    licensed: Callable[..., None], legs: _Legs
) -> None:
    licensed("stopping")
    assert hr.run(_HOLDER, _AT) == 0
    assert legs.calls == ["stop", "start", "resume"]
    assert legs.notes == ["completed: stop/start/resume finished"]


@pytest.mark.parametrize("phase", ["stopped", "starting"])
def test_stopped_and_starting_hold_go_straight_to_start(
    phase: str, licensed: Callable[..., None], legs: _Legs
) -> None:
    """The stop is already complete (or was never interrupted): no second stop."""
    licensed(phase)
    assert hr.run(_HOLDER, _AT) == 0
    assert legs.calls == ["start", "resume"]


def test_a_failed_start_leg_fails_the_attempt(licensed: Callable[..., None], legs: _Legs) -> None:
    licensed("stopped")
    legs.start_rc = 5
    assert hr.run(_HOLDER, _AT) == 1
    assert legs.calls == ["start"]  # resume must not run behind a failed start
    assert legs.notes[0].startswith("failed at start: RuntimeError('start leg exited 5'")


def test_a_failed_stop_leg_fails_the_attempt(licensed: Callable[..., None], legs: _Legs) -> None:
    licensed("stopping")
    legs.stop_rc = 3
    assert hr.run(_HOLDER, _AT) == 1
    assert legs.calls == ["stop"]
    assert legs.notes[0].startswith("failed at stop: RuntimeError('stop leg exited 3'")


def test_a_failed_resume_leg_fails_the_attempt(
    licensed: Callable[..., None], legs: _Legs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held unit with services up is not done: the hold must be released too."""
    licensed("stopped")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("resume refused")

    monkeypatch.setattr("cli.commands._maintenance._resume", _boom)
    assert hr.run(_HOLDER, _AT) == 1
    assert legs.notes[0].startswith("failed at resume: RuntimeError('resume refused'")


def test_a_hold_that_moved_on_is_refused(licensed: Callable[..., None], legs: _Legs) -> None:
    """Another generation holds this unit now: completing it is not our call."""
    licensed("stopping", holder="other-host:pid1")
    assert hr.run(_HOLDER, _AT) == 1
    assert legs.calls == []
    assert legs.notes == ["refused: this unit is not held by the supplied maintenance generation"]


def test_no_hold_is_refused(monkeypatch: pytest.MonkeyPatch, legs: _Legs) -> None:
    from shared import maintenance

    monkeypatch.setattr(maintenance, "snapshot", lambda: None)
    assert hr.run(_HOLDER, _AT) == 1
    assert legs.calls == [] and legs.notes[0].startswith("refused:")


@pytest.mark.parametrize("phase", ["preparing", "draining", "drained", "ready"])
def test_pre_stop_phases_are_refused(
    phase: str, licensed: Callable[..., None], legs: _Legs
) -> None:
    licensed(phase)
    assert hr.run(_HOLDER, _AT) == 1
    assert legs.calls == []
    assert legs.notes[0].startswith(f"refused: hold phase '{phase}'")


def test_a_cleared_verdict_is_refused(
    licensed: Callable[..., None], legs: _Legs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The updater outcome left the failure window while the attempt travelled."""
    licensed("stopping")
    monkeypatch.setattr(sp, "stranded_hold_verdict", _verdict_reader("clear"))
    assert hr.run(_HOLDER, _AT) == 1
    assert legs.calls == []


def test_the_kill_switch_refuses_the_attempt(
    licensed: Callable[..., None], legs: _Legs, monkeypatch: pytest.MonkeyPatch
) -> None:
    licensed("stopping")
    monkeypatch.setattr(settings.gateway, "stranded_hold_recovery", False)
    assert hr.run(_HOLDER, _AT) == 1
    assert legs.calls == []


def test_a_failed_outcome_write_does_not_change_the_verdict(
    licensed: Callable[..., None], legs: _Legs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record is evidence, not the outcome: a DB blip must not fail a good run."""
    licensed("stopped")

    def _boom(_note: str) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr("shared.host_deploy_state.finish_stranded_recovery", _boom)
    assert hr.run(_HOLDER, _AT) == 0
    assert legs.calls == ["start", "resume"]


def test_main_refuses_a_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        hr._main(["prog", "--operation", _HOLDER, "--acquired-at", "2026-09-12T03:00:00"])
