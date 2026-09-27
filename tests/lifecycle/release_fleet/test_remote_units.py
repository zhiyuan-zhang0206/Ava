"""A coordinator and a remote unit's follower over the real channel (in process).

Barriers, the abort and recovery matrix with a remote unit, a failed and a
silent unit, an excluded unit, and a death of either executor after each of
its durable journal writes, reconciled by instruction digest.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from cli.release_transition import journal as journal_module
from cli.release_transition.journal import Operation, create, exclusive, read_operation
from tests.lifecycle.release_fleet.remote import (
    GATEWAY_AGENT,
    RUNNER_AGENT,
    ControllerLost,
    Exchange,
    Gateway,
    Unit,
    UnitEffects,
    coordinate,
    fleet_request,
    run,
    units,
)

_FORWARD = [
    ("quiescing", "candidate"),
    ("stopping", "candidate"),
    ("selecting", "candidate"),
    ("starting", "candidate"),
    ("observing", "candidate"),
    ("resuming", "candidate"),
]


def _unit_outcome(operation: Operation | None) -> str | None:
    assert operation is not None and operation.unit is not None
    return operation.unit.outcome


def test_a_clean_release_moves_the_unit_through_every_instruction(tmp_path: Path) -> None:
    request = fleet_request(tmp_path.resolve())
    effects, exchange = UnitEffects(), Exchange()
    unit = Unit(Path(request.home), effects, exchange)
    final = run(request, Gateway(request), unit)
    unit_final = unit.join()
    assert final.fleet is not None and final.fleet.outcome == "clean", final.error
    assert _unit_outcome(unit_final) == "clean"
    assert effects.events == _FORWARD
    # The unit asked for exactly the candidate's authorized generation (dbgen-8's exchange).
    issue = final.issue("candidate")
    assert issue is not None and exchange.generations == [issue.number]
    cohort = final.fleet.cohort
    assert cohort is not None
    assert cohort.members == {GATEWAY_AGENT: request.gateway, RUNNER_AGENT: request.units[0].unit}
    status = final.fleet.units[0]
    assert (status.inclusion, status.answered) == ("included", "completed")
    # Every instruction the unit acted on was acted on exactly once (a `wait`
    # replaced before the unit pulled it is never acted on at all).
    assert unit_final is not None and unit_final.unit is not None
    acted = unit_final.unit.acted
    assert status.instruction is not None
    assert len(acted) == len(set(acted)) <= status.instruction.sequence
    assert acted[-1] == status.instruction.digest


def test_a_gateway_failure_before_the_fence_restores_the_unit_too(tmp_path: Path) -> None:
    request = fleet_request(tmp_path.resolve())
    effects = UnitEffects()
    unit = Unit(Path(request.home), effects, Exchange())
    final = run(request, Gateway(request, fail="stopping"), unit)
    unit_final = unit.join()
    assert final.fleet is not None and final.fleet.outcome == "aborted"
    assert final.db_fences == () and final.db_issues == ()
    assert _unit_outcome(unit_final) == "aborted"
    assert unit_final is not None and unit_final.unit is not None
    assert [d.kind for d in unit_final.unit.decisions] == ["abort"]
    assert effects.events == [
        ("quiescing", "candidate"),
        ("stopping", "candidate"),
        ("restoring", "candidate"),
    ]


def test_a_unit_failing_its_drain_aborts_the_whole_release_before_the_fence(
    tmp_path: Path,
) -> None:
    request = fleet_request(tmp_path.resolve())
    effects = UnitEffects(fail="quiescing")
    unit = Unit(Path(request.home), effects, Exchange())
    gateway = Gateway(request)
    final = run(request, gateway, unit)
    unit_final = unit.join()
    assert final.fleet is not None and final.fleet.outcome == "aborted"
    decision = final.fleet.decisions[0]
    assert decision.phase == "quiescing" and "failed or did not answer" in decision.reason
    assert final.db_fences == ()
    assert [phase for phase, _ in gateway.events] == ["quiescing", "restoring"]
    assert _unit_outcome(unit_final) == "aborted"
    assert effects.events[-1] == ("restoring", "candidate")


def test_a_gateway_start_failure_recovers_the_unit_on_the_new_generation(tmp_path: Path) -> None:
    request = fleet_request(tmp_path.resolve())
    effects, exchange = UnitEffects(), Exchange()
    unit = Unit(Path(request.home), effects, exchange)
    final = run(request, Gateway(request, fail="starting"), unit)
    unit_final = unit.join()
    assert final.fleet is not None and final.fleet.outcome == "recovered"
    assert _unit_outcome(unit_final) == "recovered"
    assert unit_final is not None and unit_final.direction == "previous"
    # The unit never started the candidate: the gateway failed before the start barrier.
    assert effects.events == [
        ("quiescing", "candidate"),
        ("stopping", "candidate"),
        ("stopping", "previous"),
        ("selecting", "previous"),
        ("starting", "previous"),
        ("observing", "previous"),
        ("resuming", "previous"),
    ]
    issue = final.issue("previous")
    assert issue is not None and exchange.generations == [issue.number]


def test_a_unit_failing_after_the_fence_is_marked_and_the_release_commits_degraded(
    tmp_path: Path,
) -> None:
    request = fleet_request(tmp_path.resolve())
    unit = Unit(Path(request.home), UnitEffects(), Exchange(refuse=True))
    final = run(request, Gateway(request), unit)
    assert final.fleet is not None and final.fleet.outcome == "degraded"
    status = final.fleet.units[0]
    assert status.inclusion == "failed" and "starting_units" in (status.reason or "")
    assert "unit_failed" in [record.alert.kind for record in final.fleet.alerts]
    unit.join()


def test_a_silent_unit_before_the_fence_aborts_the_release(tmp_path: Path) -> None:
    request = fleet_request(tmp_path.resolve(), start_s=2)
    unit = Unit(Path(request.home), UnitEffects(), Exchange())
    unit.silent = True
    gateway = Gateway(request)
    final = run(request, gateway, unit)
    assert final.fleet is not None and final.fleet.outcome == "aborted"
    decision = final.fleet.decisions[0]
    assert decision.phase == "dispatching" and "did not answer" in decision.reason
    # Nothing on the gateway was disturbed; the silent unit is left stale.
    assert [phase for phase, _ in gateway.events] == ["restoring"]
    assert final.fleet.units[0].inclusion == "unknown"


def test_an_excluded_unit_is_never_dispatched_and_stays_stale(tmp_path: Path) -> None:
    request = fleet_request(tmp_path.resolve(), excluded_only=True)
    unit = Unit(Path(request.home), UnitEffects(), Exchange())
    gateway = Gateway(request)
    final = run(request, gateway, unit)
    assert final.fleet is not None and final.fleet.outcome == "clean"
    assert unit.request is None  # never dispatched
    assert [c.stale_units for c in gateway.published] == [(request.excluded[0].unit,)]


def _writes(monkeypatch: pytest.MonkeyPatch, kind: str, die_after: int | None) -> list[str]:
    real = journal_module._write
    seen: list[str] = []

    def counted(operation: Operation) -> None:
        real(operation)
        # A unit's writes count in its executor only: its submission is the ops handoff.
        executor = kind == "fleet" or threading.current_thread().name == "unit-executor"
        if operation.request.kind == kind and executor:
            seen.append(operation.phase)
            if die_after is not None and len(seen) == die_after:
                raise ControllerLost(f"{kind} after write {die_after}")

    monkeypatch.setattr(journal_module, "_write", counted)
    return seen


def _count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> int:
    request = fleet_request(tmp_path / "count")
    counted = _writes(monkeypatch, kind, None)
    unit = Unit(Path(request.home), UnitEffects(), Exchange())
    final = run(request, Gateway(request), unit)
    unit.join()
    monkeypatch.setattr(journal_module, "_write", _real_write)
    assert final.fleet is not None and final.fleet.outcome == "clean"
    return len(counted)


_real_write = journal_module._write


def test_a_coordinator_death_after_each_durable_write_is_reconciled_by_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit keeps its phase while the coordinator is away; the continuation
    rebinds the listener and reissues nothing the unit already acted on."""
    root = tmp_path.resolve()
    (root / "count").mkdir()
    total = _count(root, monkeypatch, "fleet")
    for boundary in range(1, total + 1):
        base = root / f"b{boundary}"
        base.mkdir()
        request = fleet_request(base)
        effects = UnitEffects()
        unit = Unit(Path(request.home), effects, Exchange())
        gateway = Gateway(request)
        create(request)
        _writes(monkeypatch, "fleet", boundary)
        try:
            with exclusive(request.path) as journal:
                coordinate(journal, gateway, units(request, unit))
        except ControllerLost:
            monkeypatch.setattr(journal_module, "_write", _real_write)
            with exclusive(request.path) as journal:
                coordinate(journal, gateway, units(request, unit))
        monkeypatch.setattr(journal_module, "_write", _real_write)
        final = read_operation(request.path)
        unit_final = unit.join((boundary, final.phase, final.fleet and final.fleet.units))
        assert final.fleet is not None and final.fleet.outcome == "clean", (boundary, final.error)
        assert _unit_outcome(unit_final) == "clean", boundary
        assert effects.events == _FORWARD, boundary


def test_a_unit_executor_death_after_each_durable_write_is_reconciled_by_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    (root / "count").mkdir()
    total = _count(root, monkeypatch, "unit")
    for boundary in range(1, total + 1):
        base = root / f"b{boundary}"
        base.mkdir()
        request = fleet_request(base)
        unit = Unit(Path(request.home), UnitEffects(), Exchange())
        _writes(monkeypatch, "unit", boundary)
        final = run(request, Gateway(request), unit)
        unit_final = unit.join()
        monkeypatch.setattr(journal_module, "_write", _real_write)
        assert final.fleet is not None and final.fleet.outcome == "clean", (boundary, final.error)
        assert _unit_outcome(unit_final) == "clean", boundary
        assert unit_final is not None and unit_final.unit is not None
        acted = unit_final.unit.acted
        assert len(acted) == len(set(acted)), boundary


def test_a_continuation_gives_units_time_to_answer_again_before_calling_them_silent(
    tmp_path: Path,
) -> None:
    """Answers sent while the coordinator was away were never received; a new
    run's listener waits one re-answer window past its own bind before a unit
    whose journaled deadline long passed counts as silent."""
    from datetime import UTC, datetime, timedelta

    from cli.release_fleet.units import RemoteUnits
    from tests.lifecycle.release_fleet.fakes import Clock

    request = fleet_request(tmp_path.resolve())
    unit = Unit(Path(request.home), UnitEffects(), Exchange())
    unit.silent = True
    create(request)
    clock = Clock(datetime.now(UTC))
    first = RemoteUnits(request, transport=unit, clock=clock, reanswer_s=30)
    with exclusive(request.path) as journal:
        first.instruct(journal, "standby", bound_s=1)
    first.close()
    clock.sleep(3600)  # the coordinator was away for an hour
    later = RemoteUnits(request, transport=unit, clock=clock, reanswer_s=30)
    try:
        with exclusive(request.path) as journal:
            later.instruct(journal, "standby", bound_s=1)  # same order: rebinds, keeps the deadline
            status = later.included(journal)[0]
            assert status.instruction is not None
            assert status.instruction.deadline is not None
            assert clock() - status.instruction.deadline > timedelta(minutes=59)
            assert not later._late(status)
            clock.sleep(31)
            assert later._late(status)
    finally:
        later.close()
