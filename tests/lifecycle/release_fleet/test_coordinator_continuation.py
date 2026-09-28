"""What a held or interrupted fleet operation keeps, and what a continuation re-decides.

An operator continuation (`ava cluster update --prepared` after a hold) is
explicit; a continuation after executor death resumes a decision already
journaled. Each hold is its own alert, and the watch window never forgets
what it already saw. The harness is `test_coordinator.py`'s.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from cli.release_fleet.request import FleetRequest
from cli.release_transition.journal import create, exclusive, read_operation
from tests.lifecycle.release_fleet.fakes import Clock, drive
from tests.lifecycle.release_fleet.test_coordinator import _WHEN, Effects
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
