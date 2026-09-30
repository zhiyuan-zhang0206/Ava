"""The fleet request, the unit request and the journal progress records.

Pure model rules: which fleets a request can describe, which phase follows
which, when an abort or a recovery may be decided, and which journal writes
are successors of which (histories only grow, captured facts never change).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cli.release_fleet.policy import FleetPolicy, UnitCohort, UnitKey, capture_cohort
from cli.release_fleet.progress import (
    Decision,
    FleetProgress,
    Instruction,
    Report,
    UnitProgress,
    UnitStatus,
    decision_target,
    initial_progress,
    next_phase,
)
from cli.release_fleet.request import (
    CoordinatorEndpoint,
    Exclusion,
    FleetRequest,
    UnitRequest,
    UnitSpec,
)
from cli.release_transition.request import ReleaseRef

_WHEN = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
_GATEWAY_HOME = "/home/zzy/.ava"
_RUNNER = UnitKey(machine="macbook-air", home="/Users/zzy/.ava")
_WINDOWS = UnitKey(machine="win", home="C:\\Users\\zzy\\.ava")


def _ref(artifact: str, commit: str) -> ReleaseRef:
    return ReleaseRef(
        artifact_digest=artifact * 64,
        manifest_digest="b" * 64,
        schema_digest="c" * 64,
        source_commit=commit * 40,
    )


_PREVIOUS = _ref("a", "d")
_CANDIDATE = _ref("e", "9")


def _spec(unit: UnitKey = _RUNNER, **changes: Any) -> UnitSpec:
    fields: dict[str, Any] = {
        "unit": unit,
        "roles": ("agent-runner",),
        "adapter": "darwin-launchd-v1",
        "previous": _ref("1", "d"),
        "candidate": _ref("2", "9"),
        "configuration_digest": "f" * 64,
        "sql_inventory_digest": "0" * 64,
        "receipt_digest": "7" * 64,
        "enrollment_id": "a" * 32,
    }
    return UnitSpec.model_validate(fields | changes)


def _request(**changes: Any) -> FleetRequest:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "home": _GATEWAY_HOME,
        "created_at": _WHEN,
        "machine": "ubuntu",
        "previous": _PREVIOUS,
        "candidate": _CANDIDATE,
        "executor": _CANDIDATE,
        "configuration_digest": "f" * 64,
    }
    return FleetRequest.model_validate(fields | changes)


_ENDPOINT = CoordinatorEndpoint(host="10.0.0.5", port=8121)


# ── the fleet request ────────────────────────────────────────────────────────


def test_a_single_box_is_a_fleet_of_one() -> None:
    request = _request()
    assert (request.kind, request.units, request.excluded, request.coordinator) == (
        "fleet",
        (),
        (),
        None,
    )
    assert request.gateway == UnitKey(machine="ubuntu", home=_GATEWAY_HOME)
    assert request.policy == FleetPolicy()


def test_remote_units_name_the_coordinator_and_derive_their_own_home_request() -> None:
    policy = FleetPolicy(drain_s=45, close_s=12, cancel_grace_s=4)
    request = _request(units=(_spec(),), coordinator=_ENDPOINT, policy=policy)
    unit = request.unit_request(_RUNNER)
    assert isinstance(unit, UnitRequest) and unit.kind == "unit"
    # The maintenance hold identity (fleet id, created_at) is the same on every unit.
    assert (unit.id, unit.created_at) == (request.id, request.created_at)
    assert (unit.home, unit.machine, unit.unit) == (_RUNNER.home, _RUNNER.machine, _RUNNER)
    assert unit.executor == unit.candidate == request.spec(_RUNNER).candidate
    assert (unit.gateway, unit.coordinator, unit.policy) == (request.gateway, _ENDPOINT, policy)
    assert unit.enrollment_id == "a" * 32
    with pytest.raises(KeyError):
        request.unit_request(_WINDOWS)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"units": (_spec(),)}, "coordinator endpoint exists exactly when"),
        ({"coordinator": _ENDPOINT}, "coordinator endpoint exists exactly when"),
        (
            {
                "units": (_spec(),),
                "excluded": (Exclusion(unit=_RUNNER, reason="offline", recorded_by="operator"),),
                "coordinator": _ENDPOINT,
            },
            "either included or excluded",
        ),
        (
            {
                "excluded": (
                    Exclusion(
                        unit=UnitKey(machine="ubuntu", home=_GATEWAY_HOME),
                        reason="operator",
                        recorded_by="operator",
                    ),
                )
            },
            "never a listed unit",
        ),
        (
            {"units": (_spec(_WINDOWS), _spec(_RUNNER)), "coordinator": _ENDPOINT},
            "sorted and unique",
        ),
        (
            {"units": (_spec(candidate=_ref("2", "8")),), "coordinator": _ENDPOINT},
            "prepared a different release",
        ),
        (
            {"candidate": _ref("e", "d"), "executor": _ref("e", "d")},
            "two distinct source commits",
        ),
    ],
)
def test_a_request_describes_one_coherent_fleet(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _request(**changes)


def test_remote_unit_specs_are_runners_with_a_distinct_candidate() -> None:
    with pytest.raises(ValidationError, match="include agent-runner"):
        _spec(roles=("observability-station",))
    with pytest.raises(ValidationError):
        _spec(roles=("gateway", "agent-runner"))
    with pytest.raises(ValidationError, match="no distinct candidate"):
        _spec(candidate=_ref("1", "9"))
    assert _spec(unit=_WINDOWS).unit.home == "C:\\Users\\zzy\\.ava"


def test_the_endpoint_brackets_an_ipv6_host() -> None:
    assert _ENDPOINT.url == "http://10.0.0.5:8121"
    assert CoordinatorEndpoint(host="fd7a::1", port=8121).url == "http://[fd7a::1]:8121"
    with pytest.raises(ValidationError):
        CoordinatorEndpoint(host="evil/path", port=8121)


# ── phases, aborts and recoveries ─────────────────────────────────────────────


def _walk(kind: str, start: str, direction: str) -> list[str]:
    phases = [start]
    while (after := next_phase(kind, phases[-1], direction)) is not None:  # type: ignore[arg-type]
        phases.append(after)
    return phases


def test_the_fleet_phase_orders() -> None:
    assert _walk("fleet", "prepared", "candidate") == [
        "prepared",
        "dispatching",
        "quiescing",
        "stopping",
        "fencing",
        "selecting",
        "authorizing",
        "starting",
        "observing",
        "starting_units",
        "resuming",
        "watching",
        "complete",
    ]
    # A recovery never watches: it completes as `recovered` after resuming.
    assert _walk("fleet", "quiescing", "previous")[-3:] == [
        "starting_units",
        "resuming",
        "complete",
    ]
    assert _walk("fleet", "restoring", "candidate") == ["restoring", "complete"]
    assert next_phase("fleet", "dispatching", "previous") is None


def test_the_unit_phase_order_never_fences() -> None:
    assert _walk("unit", "prepared", "candidate") == [
        "prepared",
        "quiescing",
        "stopping",
        "authorizing",
        "selecting",
        "starting",
        "observing",
        "resuming",
        "complete",
    ]


@pytest.mark.parametrize("phase", ["prepared", "dispatching", "quiescing", "stopping"])
def test_only_a_failure_before_the_fence_aborts(phase: str) -> None:
    assert decision_target("fleet", phase, "abort", renewed=False) == "restoring"
    with pytest.raises(ValueError, match="keeps its maintenance hold"):
        decision_target("fleet", phase, "abort", renewed=True)
    with pytest.raises(ValueError, match="cannot recover from"):
        decision_target("fleet", phase, "recover", renewed=False)


@pytest.mark.parametrize(
    "phase", ["fencing", "selecting", "authorizing", "resuming", "restoring", "complete"]
)
def test_the_fence_and_the_ledger_decide_neither_abort_nor_recovery(phase: str) -> None:
    with pytest.raises(ValueError, match="cannot abort"):
        decision_target("fleet", phase, "abort", renewed=False)
    with pytest.raises(ValueError, match="cannot recover"):
        decision_target("fleet", phase, "recover", renewed=False)


@pytest.mark.parametrize(
    ("phase", "renewed", "target"),
    [
        ("starting", False, "stopping"),
        ("observing", False, "stopping"),
        ("starting_units", False, "stopping"),
        ("watching", True, "quiescing"),
    ],
)
def test_a_recovery_drains_again_exactly_when_admission_reopened(
    phase: str, *, renewed: bool, target: str
) -> None:
    assert decision_target("fleet", phase, "recover", renewed=renewed) == target
    with pytest.raises(ValueError, match="new maintenance hold exactly when"):
        decision_target("fleet", phase, "recover", renewed=not renewed)


# ── instructions and reports ─────────────────────────────────────────────────


def _instruction(**changes: Any) -> Instruction:
    fields: dict[str, Any] = {
        "operation": uuid4(),
        "unit": _RUNNER,
        "sequence": 1,
        "action": "quiesce",
        "direction": "candidate",
        "image": _PREVIOUS.selector,
        "maintenance_at": _WHEN,
    }
    return Instruction.model_validate(fields | changes)


def test_an_instruction_is_named_by_its_digest_and_answered_by_its_states() -> None:
    first = _instruction()
    assert first.digest == Instruction.model_validate_json(first.model_dump_json()).digest
    assert first.digest != first.model_copy(update={"sequence": 2}).digest
    assert first.answered_by("drained") and not first.answered_by("closed")
    assert _instruction(action="wait").answered_by("closed")
    assert _instruction(action="restore").answered_by("restored")


def test_a_report_answers_one_instruction_coherently() -> None:
    instruction = _instruction()
    cohort = UnitCohort(unit=_RUNNER, agents=(3, 5), reaped=(5,))
    base = {
        "operation": instruction.operation,
        "unit": _RUNNER,
        "instruction": instruction.digest,
        "at": _WHEN,
    }
    drained = Report.model_validate(base | {"state": "drained", "cohort": cohort})
    assert drained.cohort == cohort
    with pytest.raises(ValidationError, match="drained report carries"):
        Report.model_validate(base | {"state": "drained"})
    with pytest.raises(ValidationError, match="drained report carries"):
        Report.model_validate(base | {"state": "closed", "cohort": cohort})
    with pytest.raises(ValidationError, match="its own unit"):
        Report.model_validate(
            base | {"state": "drained", "cohort": cohort.model_copy(update={"unit": _WINDOWS})}
        )
    with pytest.raises(ValidationError, match="explains itself"):
        Report.model_validate(base | {"state": "failed"})
    with pytest.raises(ValidationError, match="evidence exceeds"):
        Report.model_validate(base | {"state": "closed", "evidence": {"blob": "x" * 20000}})


def test_a_unit_status_records_only_answers_to_its_current_instruction() -> None:
    instruction = _instruction()
    report = Report(
        operation=instruction.operation,
        unit=_RUNNER,
        instruction=instruction.digest,
        state="failed",
        at=_WHEN,
        detail="drain refused",
    )
    status = UnitStatus(unit=_RUNNER, instruction=instruction, report=report)
    assert status.answered == "failed"
    stale = report.model_copy(update={"instruction": "0" * 64})
    with pytest.raises(ValidationError, match="answers the unit's current instruction"):
        UnitStatus(unit=_RUNNER, instruction=instruction, report=stale)
    with pytest.raises(ValidationError, match="records why"):
        UnitStatus(unit=_RUNNER, inclusion="failed")


# ── journal successors ───────────────────────────────────────────────────────


def _fleet_progress() -> FleetProgress:
    request = _request(units=(_spec(),), coordinator=_ENDPOINT)
    progress = initial_progress(request)["fleet"]
    assert isinstance(progress, FleetProgress)
    return progress


def test_initial_fleet_progress_lists_every_unit_as_captured() -> None:
    exclusion = Exclusion(unit=_WINDOWS, reason="paused", recorded_by="request", detail="win")
    request = _request(units=(_spec(),), excluded=(exclusion,), coordinator=_ENDPOINT)
    progress = initial_progress(request)["fleet"]
    assert isinstance(progress, FleetProgress)
    assert progress.maintenance_at == request.created_at
    assert [(s.unit, s.inclusion, s.reason) for s in progress.units] == [
        (_RUNNER, "included", None),
        (_WINDOWS, "excluded", "paused: win"),
    ]
    unit = initial_progress(request.unit_request(_RUNNER))["unit"]
    assert unit == UnitProgress(maintenance_at=request.created_at)


def test_fleet_histories_only_grow_and_captured_facts_never_change() -> None:
    before = _fleet_progress()
    cohort = capture_cohort(
        gateway=UnitKey(machine="ubuntu", home=_GATEWAY_HOME),
        reports=[UnitCohort(unit=UnitKey(machine="ubuntu", home=_GATEWAY_HOME), agents=(1,))],
        captured_at=_WHEN,
    )
    after = before.model_copy(update={"cohort": cohort})
    before.require_successor(after)
    other = cohort.model_copy(update={"captured_at": _WHEN + timedelta(seconds=1)})
    with pytest.raises(ValueError, match="never changes"):
        after.require_successor(after.model_copy(update={"cohort": other}))
    with pytest.raises(ValueError, match="only a recovery decision takes a new maintenance hold"):
        before.require_successor(before.model_copy(update={"maintenance_at": _WHEN + timedelta(1)}))
    decision = Decision(kind="recover", phase="watching", reason="core failed", at=_WHEN)
    decided = before.decided(decision, _WHEN + timedelta(minutes=5))
    before.require_successor(decided)
    with pytest.raises(ValueError, match="append-only"):
        decided.require_successor(
            before.model_copy(update={"maintenance_at": decided.maintenance_at})
        )
    with pytest.raises(ValidationError, match="at most one abort or one recovery"):
        decided.model_copy(update={"decisions": (decision, decision)}).model_validate(
            decided.model_dump() | {"decisions": (decision, decision)}
        )


def test_a_unit_never_returns_once_it_left_the_operation() -> None:
    before = _fleet_progress()
    failed = before.model_copy(
        update={
            "units": (
                before.units[0].model_copy(update={"inclusion": "failed", "reason": "start"}),
            )
        }
    )
    before.require_successor(failed)
    excluded = failed.model_copy(
        update={
            "units": (
                failed.units[0].model_copy(update={"inclusion": "excluded", "reason": "operator"}),
            )
        }
    )
    failed.require_successor(excluded)
    with pytest.raises(ValueError, match="cannot return to the operation"):
        excluded.require_successor(before)
    with pytest.raises(ValueError, match="units never change"):
        before.require_successor(before.model_copy(update={"units": ()}))


def test_a_unit_acts_on_an_instruction_only_once_and_never_on_an_older_one() -> None:
    first = _instruction()
    second = _instruction(operation=first.operation, sequence=2, action="close")
    progress = UnitProgress(maintenance_at=_WHEN, instruction=first, acted=(first.digest,))
    newer = progress.model_copy(
        update={"instruction": second, "acted": (first.digest, second.digest)}
    )
    progress.require_successor(newer)
    with pytest.raises(ValueError, match="older instruction"):
        newer.require_successor(progress.model_copy(update={"acted": newer.acted}))
    with pytest.raises(ValueError, match="append-only"):
        newer.require_successor(progress)
    with pytest.raises(ValidationError, match="last one acted on"):
        UnitProgress(maintenance_at=_WHEN, instruction=second, acted=(first.digest,))
