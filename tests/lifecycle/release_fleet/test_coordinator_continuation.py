"""What a held or interrupted fleet operation keeps, and what a continuation re-decides.

An operator continuation (`ava cluster update --prepared` after a hold) is
explicit; a continuation after executor death resumes a decision already
journaled. Each hold is its own alert, and the watch window never forgets
what it already saw. The harness is `test_coordinator.py`'s.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cli.release_fleet.gateway import Samples
from cli.release_fleet.policy import Cohort
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


class FatalTurnsMidWindow(Effects):
    """Chosen watch samples show a cohort agent's fatal turn; a completed turn
    clears the mark (`last_turn_fatal_at`), so later samples show none."""

    def __init__(self, request: FleetRequest, *, fatal: dict[int, tuple[int, ...]]) -> None:
        super().__init__(request)
        self.cohort_agents = (7, 8, 9)
        self.fatal = fatal  # watch sample number -> agents whose fatal turn it shows
        self.watch_samples = 0

    def sample(
        self,
        operation: Operation,
        cohort: Cohort,
        *,
        since: datetime,
        observed: datetime,
        agents: bool,
    ) -> Samples:
        base = super().sample(operation, cohort, since=since, observed=observed, agents=agents)
        if not agents:
            return base
        self.watch_samples += 1
        failing = self.fatal.get(self.watch_samples, ())
        shown = tuple(
            a.model_copy(update={"runtime_error": a.agent in failing}) for a in base.agents
        )
        return base._replace(agents=shown)


def test_an_agent_affected_mid_window_keeps_the_release_from_known_good(
    request_record: FleetRequest,
) -> None:
    """A fatal turn seen once, then cleared by a completed turn, still marks
    the release degraded: any affected agent does, and nothing it showed is
    forgotten by a later clean sample."""
    create(request_record)
    effects = FatalTurnsMidWindow(request_record, fatal={1: (7,)})
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    final = read_operation(request_record.path)
    assert effects.watch_samples > 2
    assert final.fleet is not None and final.fleet.outcome == "degraded"
    commit = final.fleet.verdicts[-1]
    assert [(a.agent, a.reasons) for a in commit.affected] == [(7, ("runtime_error",))]
    assert [c.outcome for c in effects.published] == ["degraded"]
    assert not effects.published[0].exercised


def test_errors_spread_over_the_window_add_up_to_the_threshold(
    request_record: FleetRequest,
) -> None:
    """Each agent fails once, in a different sample, each cleared before the
    next: together they exceed the threshold, and the candidate recovers."""
    create(request_record)
    effects = FatalTurnsMidWindow(request_record, fatal={1: (7,), 2: (8,)})
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    final = read_operation(request_record.path)
    assert final.fleet is not None and final.fleet.outcome == "recovered"
    decision = final.fleet.decisions[0]
    assert (decision.kind, decision.phase) == ("recover", "watching")


def test_a_mass_failure_of_a_large_cohort_still_recovers(request_record: FleetRequest) -> None:
    """Every agent of a 2,000-agent cohort shows a fatal turn in the first watch
    sample: what the window saw and the recover verdict both fit the journal,
    so the candidate recovers instead of holding at `watching`."""
    create(request_record)
    cohort = tuple(range(1, 2001))
    effects = FatalTurnsMidWindow(request_record, fatal={1: cohort})
    effects.cohort_agents = cohort
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    final = read_operation(request_record.path)
    assert final.fleet is not None and final.fleet.outcome == "recovered"
    assert [(d.kind, d.phase) for d in final.fleet.decisions] == [("recover", "watching")]
    [recover] = [v for v in final.fleet.verdicts if v.action == "recover"]
    assert [a.agent for a in recover.affected] == list(cohort)


def test_what_the_window_saw_survives_executor_death(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(request_record)
    effects = FatalTurnsMidWindow(request_record, fatal={1: (7,)})
    real = journal_module.Journal.record_fleet

    def dies_after_the_sighting(self: Journal, progress: Any) -> Operation:
        written = real(self, progress)
        if progress.window_facts:
            monkeypatch.setattr(journal_module.Journal, "record_fleet", real)
            raise ControllerLost
        return written

    monkeypatch.setattr(journal_module.Journal, "record_fleet", dies_after_the_sighting)
    with pytest.raises(ControllerLost), exclusive(request_record.path) as journal:
        drive(journal, effects)
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    final = read_operation(request_record.path)
    assert final.fleet is not None and final.fleet.outcome == "degraded"


def test_an_alert_undelivered_at_completion_is_logged_and_kept_for_status(
    request_record: FleetRequest,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    """Completion does not wait for a delivery that keeps failing, but it
    never drops one silently: the error is logged, and the journal keeps the
    alert with the routes it has not reached, which `release status` shows."""
    from cli.release_operator import status as status_module
    from shared import paths as shared_paths
    from shared.cluster import machine as shared_machine

    create(request_record)
    effects = Effects(request_record, fail="starting")
    effects.undeliverable = {"recovering"}
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    assert read_operation(request_record.path).terminal
    errors = [r["message"] for r in loguru_records if r["level"].name == "ERROR"]
    assert any("recovering" in message and "undelivered" in message for message in errors)

    monkeypatch.setattr(shared_paths, "ava_home", lambda: Path(request_record.home))
    monkeypatch.setattr(shared_machine, "machine_name", lambda: request_record.machine)

    def nothing_selected(_home: Path) -> None:
        return None

    monkeypatch.setattr(status_module, "current_release", nothing_selected)
    body = status_module._status_body(operation=str(request_record.id))
    alerts = body["operation"]["fleet"]["alerts"]
    assert {a["kind"]: a["undelivered"] for a in alerts} == {
        "recovering": ["alert_row"],
        "recovered": [],
    }
