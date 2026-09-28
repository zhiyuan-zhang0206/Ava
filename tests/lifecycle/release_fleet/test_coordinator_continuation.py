"""What a held or interrupted fleet operation keeps, and what a continuation re-decides.

An operator continuation (`ava cluster update --prepared` after a hold) is
explicit; a continuation after executor death resumes a decision already
journaled. Each hold is its own alert, and the watch window never forgets
what it already saw. The harness is `test_coordinator.py`'s.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cli.release_fleet.request import FleetRequest
from cli.release_transition import journal as journal_module
from cli.release_transition.journal import Journal, Operation, create, exclusive, read_operation
from tests.lifecycle.release_fleet.fakes import Clock, drive
from tests.lifecycle.release_fleet.test_coordinator import _WHEN, ControllerLost, Effects
from tests.lifecycle.release_fleet.test_coordinator import request_record as request_record


def _later(minutes: int) -> Clock:
    """The operator continues later; the continuation's clock has moved on."""
    return Clock(_WHEN + timedelta(minutes=minutes))


def test_every_hold_of_one_operation_raises_its_own_alert(request_record: FleetRequest) -> None:
    """A continued operation that holds again alerts again: the second hold is
    journaled as its own alert and delivered, never folded into the first."""
    create(request_record)
    effects = Effects(request_record, fail="fencing")
    with pytest.raises(RuntimeError), exclusive(request_record.path) as journal:
        drive(journal, effects)
    effects.failures["authorizing"] = RuntimeError
    with pytest.raises(RuntimeError), exclusive(request_record.path) as journal:
        drive(journal, effects, clock=_later(10))
    state = read_operation(request_record.path)
    assert state.phase == "authorizing" and state.fleet is not None
    held = [(r.alert.summary, r.delivered) for r in state.fleet.alerts if r.alert.kind == "held"]
    assert held == [
        ("held at fencing: RuntimeError: injected native failure", ("alert_row",)),
        ("held at authorizing: RuntimeError: injected native failure", ("alert_row",)),
    ]


class CoreDownAfterRecovery(Effects):
    """The candidate fails to start; the recovery's shared core stays down until repaired."""

    broken = True

    def sample(self, operation: Operation, *args: Any, **kwargs: Any) -> Any:
        down = operation.direction == "previous" and self.broken
        self.failing_signals = {"database": "refused"} if down else {}
        return super().sample(operation, *args, **kwargs)


def _held_alerts(path: Path) -> int:
    state = read_operation(path)
    assert state.fleet is not None
    return [r.alert.kind for r in state.fleet.alerts].count("held")


def test_an_operator_continuation_judges_a_held_start_barrier_again(
    request_record: FleetRequest,
) -> None:
    """A held recovery is continued by the operator after a repair: the held
    start barrier is judged again. Still failing, it holds again with a new
    alert; repaired, it proceeds and the recovery completes."""
    create(request_record)
    effects = CoreDownAfterRecovery(request_record, fail="starting")
    with pytest.raises(RuntimeError, match="held"), exclusive(request_record.path) as journal:
        drive(journal, effects)
    held = read_operation(request_record.path)
    assert (held.phase, held.direction) == ("starting_units", "previous")
    assert held.error is not None and _held_alerts(request_record.path) == 1

    with pytest.raises(RuntimeError, match="held"), exclusive(request_record.path) as journal:
        drive(journal, effects, clock=_later(10))
    assert _held_alerts(request_record.path) == 2, "a second hold alerts again"

    effects.broken = False  # the operator repaired the database
    with exclusive(request_record.path) as journal:
        drive(journal, effects, clock=_later(20))
    final = read_operation(request_record.path)
    assert final.terminal and final.fleet is not None and final.fleet.outcome == "recovered"
    assert [(v.direction, v.action) for v in final.fleet.verdicts] == [
        ("previous", "hold"),
        ("previous", "hold"),
        ("previous", "proceed"),
    ]


def test_a_hold_journaled_before_executor_death_is_executed_not_judged_again(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor died after journaling a hold verdict, before holding: its
    continuation carries out the journaled hold even though the world has
    recovered. Only an operator continuing the executed hold judges again."""
    create(request_record)
    effects = CoreDownAfterRecovery(request_record, fail="starting")
    real = journal_module.Journal.record_fleet

    def dies_after_the_hold(self: Journal, progress: Any) -> Operation:
        written = real(self, progress)
        if any(v.action == "hold" for v in progress.verdicts):
            monkeypatch.setattr(journal_module.Journal, "record_fleet", real)
            raise ControllerLost
        return written

    monkeypatch.setattr(journal_module.Journal, "record_fleet", dies_after_the_hold)
    with pytest.raises(ControllerLost), exclusive(request_record.path) as journal:
        drive(journal, effects)
    assert read_operation(request_record.path).error is None, "died before holding"

    effects.broken = False
    with pytest.raises(RuntimeError, match="held"), exclusive(request_record.path) as journal:
        drive(journal, effects, clock=_later(10))
    held = read_operation(request_record.path)
    assert held.phase == "starting_units" and held.error is not None
    assert held.fleet is not None and [v.action for v in held.fleet.verdicts] == ["hold"]
    assert _held_alerts(request_record.path) == 1

    with exclusive(request_record.path) as journal:
        drive(journal, effects, clock=_later(20))
    final = read_operation(request_record.path)
    assert final.fleet is not None and final.fleet.outcome == "recovered"
    assert [v.action for v in final.fleet.verdicts] == ["hold", "proceed"]
