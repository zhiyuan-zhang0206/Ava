"""The fleet coordinator on a fleet of one: phase order, abort, recovery, hold and crashes.

The gateway unit's effects are recorded fakes (`fakes.OffDutyGateway`) except
where a test exercises the real one-home effect; the journal, the policy
verdicts and the coordinator are real. Effects assert that their phase's
intent is durable before they run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.deploy.maintenance.state import MaintenanceHold
from cli.release_fleet.policy import FleetPolicy
from cli.release_fleet.request import FleetRequest
from cli.release_transition import journal as journal_module
from cli.release_transition.journal import (
    Direction,
    Journal,
    Operation,
    create,
    exclusive,
    read_operation,
)
from cli.release_transition.local import LocalTransition
from cli.release_transition.request import ReleaseRef
from tests.lifecycle.release_fleet.fakes import Clock, FakeLease, OffDutyGateway, drive
from tests.lifecycle.transition.phases import advance_to, journal_fence, journal_issue

_WHEN = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


@pytest.fixture
def request_record(tmp_path: Path) -> FleetRequest:
    home = tmp_path.resolve() / "home"
    home.mkdir()
    previous = ReleaseRef(
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        schema_digest="c" * 64,
        source_commit="d" * 40,
    )
    candidate = previous.model_copy(update={"artifact_digest": "e" * 64, "source_commit": "9" * 40})
    (home / "releases").mkdir()
    (home / "releases/current-release").write_text(
        json.dumps(
            {
                "artifact_digest": previous.artifact_digest,
                "manifest_digest": previous.manifest_digest,
            }
        )
    )
    return FleetRequest(
        id=uuid4(),
        home=str(home),
        registry=str(home.parent / "registry.json"),
        created_at=_WHEN,
        machine="test",
        previous=previous,
        candidate=candidate,
        executor=candidate,
        configuration_digest="f" * 64,
        policy=FleetPolicy(watch_s=120),
    )


class ControllerLost(BaseException):
    """A process death cannot run the executor's exception compensation."""


class UnlistedError(Exception):
    """An `Exception` of a class no executor names."""


# A phase failure is routed by its phase, never by its class.
_FAILURE_CLASSES = [RuntimeError, psycopg.OperationalError, UnlistedError]


class Effects(OffDutyGateway):
    def __init__(
        self,
        request: FleetRequest,
        *,
        fail: str = "",
        error: type[Exception] = RuntimeError,
        crash: bool = False,
        then_crash: str = "",
    ) -> None:
        super().__init__(request)
        self.events: list[tuple[str, Direction | None]] = []
        self.failures: dict[str, type[BaseException]] = {}
        if fail:
            self.failures[fail] = ControllerLost if crash else error
        if then_crash:
            self.failures[then_crash] = ControllerLost
        self.selector = "previous"
        self.selector_writes = 0
        self.cohort_agents = (7,)

    def _effect(self, phase: str) -> Operation:
        operation = read_operation(self.request.path)
        assert operation.phase == phase, "intent must be durable before an effect"
        self.events.append((phase, operation.direction))
        failure = self.failures.pop(phase, None)
        if failure is not None:
            raise failure("injected native failure")
        return operation

    def preflight(self) -> None:
        if read_operation(self.request.path).phase == "prepared":
            self._effect("prepared")

    def quiesce(self, operation: Operation) -> MaintenanceHold:
        assert operation == self._effect("quiescing")
        return super().quiesce(operation)

    def stop(self, operation: Operation) -> None:
        assert operation == self._effect("stopping")

    def fence(self, journal: Journal) -> None:
        assert journal.operation == self._effect("fencing")
        journal_fence(journal)

    def authorize(self, journal: Journal) -> None:
        assert journal.operation == self._effect("authorizing")
        journal_issue(journal)

    def select(self, operation: Operation) -> None:
        assert operation == self._effect("selecting")
        if self.selector != operation.direction:
            self.selector = operation.direction or "?"
            self.selector_writes += 1

    def start(self, journal: Journal) -> None:
        assert journal.operation == self._effect("starting")

    def observe(self, operation: Operation) -> None:
        assert operation == self._effect("observing")

    def resume(self, operation: Operation) -> None:
        assert operation == self._effect("resuming")

    def restore(self, journal: Journal) -> None:
        assert journal.operation == self._effect("restoring")


_CANDIDATE_EFFECTS = [
    ("prepared", "candidate"),
    ("quiescing", "candidate"),
    ("stopping", "candidate"),
    ("fencing", "candidate"),
    ("selecting", "candidate"),
    ("authorizing", "candidate"),
    ("starting", "candidate"),
    ("observing", "candidate"),
    ("resuming", "candidate"),
]


def test_clean_release_runs_every_fleet_phase_then_publishes_known_good(
    request_record: FleetRequest,
) -> None:
    create(request_record)
    effects = Effects(request_record)
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    assert effects.events == _CANDIDATE_EFFECTS
    final = read_operation(request_record.path)
    assert final.terminal and final.fleet is not None and final.fleet.outcome == "clean"
    assert [(v.stage, v.action) for v in final.fleet.verdicts] == [
        ("start", "proceed"),
        ("watch", "commit"),
    ]
    cohort = final.fleet.cohort
    assert cohort is not None and cohort.members == {7: request_record.gateway}
    assert final.fleet.admitted is not None and final.fleet.admitted.number == 0
    assert [c.outcome for c in effects.published] == ["clean"]
    assert effects.published[0].exercised
    assert effects.lease.events == ["held", "released"]
    assert effects.selector_writes == 1


def test_candidate_start_failure_closes_candidate_before_selecting_previous(
    request_record: FleetRequest,
) -> None:
    create(request_record)
    effects = Effects(request_record, fail="starting")
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    assert effects.events == [
        *_CANDIDATE_EFFECTS[:7],
        ("stopping", "previous"),
        ("fencing", "previous"),
        ("selecting", "previous"),
        ("authorizing", "previous"),
        ("starting", "previous"),
        ("observing", "previous"),
        ("resuming", "previous"),
    ]
    final = read_operation(request_record.path)
    assert final.terminal and final.direction == "previous"
    assert final.fleet is not None and final.fleet.outcome == "recovered"
    assert [d.kind for d in final.fleet.decisions] == ["recover"]
    # The failed candidate's generation 1 is fenced; the predecessor runs on 2.
    fenced = [(fence.direction, fence.generation.number) for fence in final.db_fences]
    issued = [(issue.direction, issue.number) for issue in final.db_issues]
    assert fenced == [("candidate", 0), ("previous", 1)]
    assert issued == [("candidate", 1), ("previous", 2)]
    assert [r.alert.kind for r in final.fleet.alerts] == ["recovering", "recovered"]
    assert [c.outcome for c in effects.published] == ["recovered"]
    assert effects.selector_writes == 2


@pytest.mark.parametrize("error", _FAILURE_CLASSES)
@pytest.mark.parametrize("phase", ["prepared", "quiescing", "stopping"])
def test_failure_before_the_fence_aborts_and_restores_the_unchanged_previous(
    request_record: FleetRequest, phase: str, error: type[Exception]
) -> None:
    create(request_record)
    effects = Effects(request_record, fail=phase, error=error)
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    final = read_operation(request_record.path)
    assert final.terminal and final.direction == "candidate"
    assert final.fleet is not None and final.fleet.outcome == "aborted"
    assert [(d.kind, d.phase) for d in final.fleet.decisions] == [("abort", phase)]
    assert final.fleet.decisions[0].reason == f"{error.__name__}: injected native failure"
    assert final.db_fences == () and final.db_issues == ()
    assert effects.events[-1] == ("restoring", "candidate")
    assert effects.selector_writes == 0
    assert [c.outcome for c in effects.published] == ["aborted"]


def test_dispatch_failure_aborts_before_any_unit_is_disturbed(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.release_fleet.units import RemoteUnits

    def lost(_self: RemoteUnits, _journal: Journal) -> None:
        raise RuntimeError("a unit refused dispatch")

    monkeypatch.setattr(RemoteUnits, "dispatch", lost)
    create(request_record)
    effects = Effects(request_record)
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    final = read_operation(request_record.path)
    assert final.fleet is not None and final.fleet.outcome == "aborted"
    assert [e for e, _ in effects.events] == ["prepared", "restoring"]


@pytest.mark.parametrize("error", _FAILURE_CLASSES)
@pytest.mark.parametrize("phase", ["fencing", "selecting", "authorizing", "resuming"])
def test_failure_after_the_fence_holds_and_never_invents_rollback(
    request_record: FleetRequest, phase: str, error: type[Exception]
) -> None:
    create(request_record)
    effects = Effects(request_record, fail=phase, error=error)
    with (
        pytest.raises(error, match="native failure"),
        exclusive(request_record.path) as journal,
    ):
        drive(journal, effects)
    interrupted = read_operation(request_record.path)
    detail = f"{error.__name__}: injected native failure"
    assert interrupted.phase == phase and interrupted.direction == "candidate"
    assert not interrupted.terminal and interrupted.error == detail
    assert interrupted.fleet is not None
    assert [r.alert.kind for r in interrupted.fleet.alerts] == ["held"]
    assert interrupted.fleet.alerts[0].alert.summary == f"held at {phase}: {detail}"
    assert all(direction == "candidate" for _, direction in effects.events)
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    final = read_operation(request_record.path)
    assert final.terminal and final.fleet is not None and final.fleet.outcome == "clean"
    assert effects.selector_writes == 1


@pytest.mark.parametrize(
    "phase",
    [
        "quiescing",
        "stopping",
        "fencing",
        "selecting",
        "authorizing",
        "starting",
        "observing",
        "resuming",
        "restoring",
    ],
)
def test_process_death_retains_exact_decision_for_reconciliation(
    request_record: FleetRequest, phase: str
) -> None:
    create(request_record)
    if phase == "restoring":
        effects = Effects(request_record, fail="stopping", then_crash="restoring")
    else:
        effects = Effects(request_record, fail=phase, crash=True)
    with pytest.raises(ControllerLost), exclusive(request_record.path) as journal:
        drive(journal, effects)
    interrupted = read_operation(request_record.path)
    assert interrupted.phase == phase
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    final = read_operation(request_record.path)
    assert final.terminal and final.fleet is not None
    expected = "aborted" if phase == "restoring" else "clean"
    assert final.fleet.outcome == expected
    assert effects.selector_writes == (0 if phase == "restoring" else 1)


def test_failed_recovery_remains_held_instead_of_looping_between_releases(
    request_record: FleetRequest,
) -> None:
    create(request_record)
    effects = Effects(request_record, fail="starting")
    with exclusive(request_record.path) as journal:
        advance_to(journal, "starting")
        journal.recover("candidate failed", at=_WHEN)
        with pytest.raises(RuntimeError, match="native failure"):
            drive(journal, effects)
    state = read_operation(request_record.path)
    assert state.direction == "previous" and state.phase == "starting" and not state.terminal
    assert all(direction == "previous" for _, direction in effects.events)


def test_watch_failure_recovers_under_a_new_maintenance_hold(
    request_record: FleetRequest,
) -> None:
    """Admission had reopened: the recovery drains again before stopping the candidate."""
    create(request_record)

    class FailsWhileWatching(Effects):
        def sample(self, operation: Operation, *args: Any, **kwargs: Any) -> Any:
            self.failing_signals = (
                {"redis": "PING refused"} if operation.phase == "watching" else {}
            )
            return super().sample(operation, *args, **kwargs)

    watching = FailsWhileWatching(request_record)
    with exclusive(request_record.path) as journal:
        drive(journal, watching)
    final = read_operation(request_record.path)
    assert final.fleet is not None and final.fleet.outcome == "recovered"
    decision = final.fleet.decisions[0]
    assert (decision.kind, decision.phase) == ("recover", "watching")
    assert final.fleet.maintenance_at == decision.at != request_record.created_at
    assert watching.events[len(_CANDIDATE_EFFECTS) :] == [
        ("quiescing", "previous"),
        ("stopping", "previous"),
        ("fencing", "previous"),
        ("selecting", "previous"),
        ("authorizing", "previous"),
        ("starting", "previous"),
        ("observing", "previous"),
        ("resuming", "previous"),
    ]
    kinds = [r.alert.kind for r in final.fleet.alerts]
    assert kinds == ["recovering", "recovered"]


def test_previous_direction_start_failure_holds_instead_of_recovering_twice(
    request_record: FleetRequest,
) -> None:
    create(request_record)

    class CoreDownAfterRecovery(Effects):
        def sample(self, operation: Operation, *args: Any, **kwargs: Any) -> Any:
            down = operation.direction == "previous"
            self.failing_signals = {"database": "refused"} if down else {}
            return super().sample(operation, *args, **kwargs)

    effects = CoreDownAfterRecovery(request_record, fail="starting")
    with (
        pytest.raises(RuntimeError, match="held for the operator"),
        exclusive(request_record.path) as journal,
    ):
        drive(journal, effects)
    state = read_operation(request_record.path)
    assert (state.phase, state.direction, state.terminal) == ("starting_units", "previous", False)
    assert state.fleet is not None
    assert [(v.stage, v.direction, v.action) for v in state.fleet.verdicts] == [
        ("start", "previous", "hold")
    ]
    assert "held" in [r.alert.kind for r in state.fleet.alerts]


def test_a_journaled_verdict_is_executed_after_death_instead_of_rejudged(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(request_record)

    class WatchFails(Effects):
        healthy = False

        def sample(self, operation: Operation, *args: Any, **kwargs: Any) -> Any:
            fails = operation.phase == "watching" and not self.healthy
            self.failing_signals = {"pooler": "pooled login refused"} if fails else {}
            return super().sample(operation, *args, **kwargs)

    effects = WatchFails(request_record)
    real = journal_module.Journal.record_fleet

    def dies_after_the_verdict(self: Journal, progress: Any) -> Operation:
        written = real(self, progress)
        if any(v.stage == "watch" for v in progress.verdicts):
            monkeypatch.setattr(journal_module.Journal, "record_fleet", real)
            raise ControllerLost
        return written

    monkeypatch.setattr(journal_module.Journal, "record_fleet", dies_after_the_verdict)
    with pytest.raises(ControllerLost), exclusive(request_record.path) as journal:
        drive(journal, effects)
    effects.healthy = True  # the world recovered; the journaled decision stands
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    final = read_operation(request_record.path)
    assert final.fleet is not None and final.fleet.outcome == "recovered"
    assert [(v.stage, v.action) for v in final.fleet.verdicts if v.direction == "candidate"] == [
        ("start", "proceed"),
        ("watch", "recover"),
    ]


def _writes(monkeypatch: pytest.MonkeyPatch, *, die_after: int | None) -> list[str]:
    """Count durable journal writes; optionally die right after the Nth one."""
    real = journal_module._write
    seen: list[str] = []

    def counted(operation: Operation) -> None:
        real(operation)
        seen.append(operation.phase)
        if die_after is not None and len(seen) == die_after:
            raise ControllerLost(f"after write {die_after}")

    monkeypatch.setattr(journal_module, "_write", counted)
    return seen


_SCENARIOS = {
    # scenario: (injected failure, outcome, decisions, selector writes)
    "clean": ("", "clean", [], 1),
    "abort": ("stopping", "aborted", ["abort"], 0),
    "recover": ("starting", "recovered", ["recover"], 2),
}


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_crash_after_every_durable_fleet_boundary_converges_on_one_outcome(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str
) -> None:
    """A death after each journal write (phase, cohort, verdict, alert, delivery,
    decision, completion) never repeats a decision: the continuation reaches the
    same outcome with the same selector writes and one publication."""
    fail, outcome, decisions, selections = _SCENARIOS[scenario]
    create(request_record)
    with exclusive(request_record.path) as journal:
        counted = _writes(monkeypatch, die_after=None)
        drive(journal, Effects(request_record, fail=fail))
    total = len(counted)
    assert total > 5
    for boundary in range(1, total + 1):
        home = tmp_path / f"b{boundary}" / "home"
        home.mkdir(parents=True)
        (home / "releases").mkdir()
        (home / "releases/current-release").write_bytes(
            (Path(request_record.home) / "releases/current-release").read_bytes()
        )
        request = request_record.model_copy(
            update={"id": uuid4(), "home": str(home), "registry": str(home.parent / "r.json")}
        )
        monkeypatch.setattr(journal_module, "_write", _unpatched_write)
        create(request)
        effects = Effects(request, fail=fail)
        _writes(monkeypatch, die_after=boundary)
        clock = Clock(_WHEN + timedelta(seconds=1))
        try:
            with exclusive(request.path) as journal:
                drive(journal, effects, clock=clock)
        except ControllerLost:
            monkeypatch.setattr(journal_module, "_write", _unpatched_write)
            with exclusive(request.path) as journal:
                drive(journal, effects, clock=clock)
        final = read_operation(request.path)
        assert final.fleet is not None and final.fleet.outcome == outcome, boundary
        assert [d.kind for d in final.fleet.decisions] == decisions, boundary
        assert effects.selector_writes == selections, boundary
        assert [c.outcome for c in effects.published] == [outcome], boundary


_unpatched_write = journal_module._write


def test_alerts_are_journaled_once_and_redelivered_until_they_land(
    request_record: FleetRequest,
) -> None:
    create(request_record)
    effects = Effects(request_record, fail="starting")
    effects.undeliverable = {"recovering"}
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    final = read_operation(request_record.path)
    assert final.fleet is not None
    records = {r.alert.kind: r.delivered for r in final.fleet.alerts}
    assert records == {"recovering": (), "recovered": ("alert_row",)}
    assert effects.delivered.count(("recovered", "alert_row")) == 1


# ── the cluster deploy lease ─────────────────────────────────────────────────


def test_a_continuation_rearms_the_deploy_lease_before_any_effect(
    request_record: FleetRequest,
) -> None:
    create(request_record)
    with pytest.raises(ControllerLost), exclusive(request_record.path) as journal:
        drive(journal, Effects(request_record, fail="fencing", crash=True))

    class LeasedFirst(Effects):
        def fence(self, journal: Journal) -> None:
            assert self.lease.events == ["held"], "an effect ran before the lease was re-armed"
            super().fence(journal)

    continuation = LeasedFirst(request_record)
    with exclusive(request_record.path) as journal:
        drive(journal, continuation)
    assert continuation.lease.events == ["held", "released"]
    final = read_operation(request_record.path)
    assert final.fleet is not None and final.fleet.outcome == "clean"


def test_a_refused_lease_aborts_before_any_unit_is_disturbed(
    request_record: FleetRequest,
) -> None:
    class Refused(FakeLease):
        def hold(self) -> None:
            raise RuntimeError("the cluster deploy lease is held by another operation")

    create(request_record)
    effects = Effects(request_record)
    effects.lease = Refused()
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    final = read_operation(request_record.path)
    assert final.fleet is not None and final.fleet.outcome == "aborted"
    assert [(d.kind, d.phase) for d in final.fleet.decisions] == [("abort", "dispatching")]
    # Nothing was quiesced, so the abort restores without the lease.
    assert [phase for phase, _ in effects.events] == ["prepared", "restoring"]


def test_a_lost_lease_holds_instead_of_acting_on_a_cluster_it_no_longer_owns(
    request_record: FleetRequest,
) -> None:
    class LosesItWhileQuiescing(Effects):
        def quiesce(self, operation: Operation) -> MaintenanceHold:
            hold = super().quiesce(operation)
            self.lease.lost = True
            return hold

    create(request_record)
    effects = LosesItWhileQuiescing(request_record)
    with (
        pytest.raises(RuntimeError, match="lost its cluster deploy lease"),
        exclusive(request_record.path) as journal,
    ):
        drive(journal, effects)
    held = read_operation(request_record.path)
    assert (held.phase, held.terminal) == ("restoring", False)
    assert held.fleet is not None and [d.phase for d in held.fleet.decisions] == ["stopping"]
    assert "held" in [r.alert.kind for r in held.fleet.alerts]
    assert [phase for phase, _ in effects.events] == ["prepared", "quiescing"]


# ── the gateway unit's real one-home effects ─────────────────────────────────


@pytest.mark.parametrize("changed", [False, True])
def test_quiescing_rechecks_predecessor_after_readonly_preflights_before_any_disruption(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch, *, changed: bool
) -> None:
    from base.deploy.maintenance import admission
    from base.deploy.release.runtime_release import VerifiedRelease
    from cli.release_transition import root_service
    from ops import agent_pause

    create(request_record)
    home = Path(request_record.home)
    transition = Effects(request_record)
    transition.previous = VerifiedRelease("a" * 64, "b" * 64, home / "a", home / "a/python", home)
    transition.candidate = VerifiedRelease("e" * 64, "b" * 64, home / "b", home / "b/python", home)
    observations: list[bool] = []
    effects: list[str] = []

    def preflight(operation: Operation, image: VerifiedRelease, *, previous: bool) -> None:
        assert operation == read_operation(request_record.path)
        assert image == (transition.previous if previous else transition.candidate)
        observations.append(previous)
        if changed and not previous:
            (home / "releases/current-release").write_text(
                json.dumps(
                    {
                        "artifact_digest": request_record.candidate.artifact_digest,
                        "manifest_digest": request_record.candidate.manifest_digest,
                    }
                )
            )

    def effect(name: str) -> Any:
        def call(*_args: object, **_kwargs: object) -> None:
            effects.append(name)

        return call

    def stop(_operation: Operation) -> None:
        effects.append("stop")
        raise RuntimeError("positive control reached stop")

    drained = SimpleNamespace(maintenance=MaintenanceHold(phase="drained"))
    monkeypatch.setattr(transition, "preflight", lambda: None)
    monkeypatch.setattr(transition, "quiesce", lambda op: LocalTransition.quiesce(transition, op))
    monkeypatch.setattr(root_service, "preflight", preflight)
    monkeypatch.setattr(agent_pause, "prepare", effect("prepare"))
    monkeypatch.setattr(agent_pause, "drain", effect("drain"))
    monkeypatch.setattr(admission, "require_operation", lambda *_args: drained)
    monkeypatch.setattr(transition, "stop", stop)
    monkeypatch.setattr(transition, "restore", lambda _journal: effects.append("restore"))
    with exclusive(request_record.path) as handle:
        drive(handle, transition)
    assert observations == [True, False]
    assert effects == (["restore"] if changed else ["prepare", "drain", "stop", "restore"])
    final = read_operation(request_record.path)
    assert final.fleet is not None and final.fleet.outcome == "aborted"
    decision = final.fleet.decisions[0]
    assert decision.phase == ("quiescing" if changed else "stopping")
    message = (
        "not the selected release before quiescing" if changed else "positive control reached stop"
    )
    assert message in decision.reason


def _reach_resuming(journal: Journal, direction: Direction) -> None:
    advance_to(journal, "starting")
    if direction == "previous":
        journal.recover("candidate failed", at=_WHEN)
    advance_to(journal, "resuming")


@pytest.mark.parametrize("direction", ["candidate", "previous"])
@pytest.mark.parametrize("pause_status", ["held", "resumed"])
@pytest.mark.parametrize("healthy", [True, False])
def test_resuming_reobserves_selected_root_before_admission_or_completion(
    request_record: FleetRequest,
    monkeypatch: pytest.MonkeyPatch,
    direction: Direction,
    pause_status: str,
    healthy: bool,
) -> None:
    from base.deploy.lifecycle import start_serving
    from base.deploy.maintenance import admission, pause_owner
    from base.deploy.release.runtime_release import VerifiedRelease
    from base.deploy.release.start_inputs import configuration_digest
    from cli.commands.lifecycle import maintenance as maintenance_commands
    from cli.release_transition import root_service

    home = Path(request_record.home)
    request = request_record.model_copy(update={"configuration_digest": configuration_digest(home)})
    create(request)
    transition = Effects(request)
    transition.previous = VerifiedRelease("a" * 64, "b" * 64, home / "a", home / "a/python", home)
    transition.candidate = VerifiedRelease("e" * 64, "b" * 64, home / "b", home / "b/python", home)
    effects: list[str] = []

    def observe(operation: Operation, image: VerifiedRelease) -> None:
        assert operation == read_operation(request.path)
        assert operation.phase == "resuming"
        assert image == (transition.candidate if direction == "candidate" else transition.previous)
        effects.append("fresh root observation")
        if not healthy:
            raise RuntimeError("selected root died after the earlier readiness observation")

    def require_holder(holder: str, at: datetime) -> Any:
        assert holder == str(request.id) and at == request.created_at
        effects.append("require maintenance owner")

    def read_pause() -> Any:
        return SimpleNamespace(
            status=pause_status, holder=str(request.id), acquired_at=request.created_at
        )

    def resume(holder: str, at: datetime, *, cancel: bool) -> None:
        assert healthy and effects[0] == "fresh root observation"
        assert holder == str(request.id) and at == request.created_at and not cancel
        effects.append("resume agents")

    monkeypatch.setattr(transition, "resume", lambda op: LocalTransition.resume(transition, op))
    monkeypatch.setattr(transition, "observe_root", lambda op: observe(op, transition.image(op)))
    monkeypatch.setattr(root_service, "observe", observe)
    monkeypatch.setattr(pause_owner, "read", read_pause)
    monkeypatch.setattr(admission, "require_operation", require_holder)
    monkeypatch.setattr(maintenance_commands, "resume", resume)
    # Deliberately leave a stale marker: it cannot substitute for observation.
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)
    with exclusive(request.path) as journal:
        _reach_resuming(journal, direction)
        if healthy:
            drive(journal, transition)
        else:
            with pytest.raises(RuntimeError, match="root died"):
                drive(journal, transition)
    final = read_operation(request.path)
    assert final.direction == direction
    if healthy:
        assert final.terminal
        assert effects == (
            ["fresh root observation"]
            if pause_status == "resumed"
            else ["fresh root observation", "require maintenance owner", "resume agents"]
        )
    else:
        assert final.phase == "resuming" and final.error is not None
        assert effects == ["fresh root observation"]


def test_selection_requires_terminal_closure_evidence_before_the_selector_moves(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal alive after the stop phase's closure holds selection."""
    from cli.commands.lifecycle import root_driver, service_stop

    create(request_record)
    transition = object.__new__(LocalTransition)
    transition.request, transition.home = request_record, Path(request_record.home)
    monkeypatch.setattr(transition, "preflight", lambda: None)
    monkeypatch.setattr(root_driver, "require_root_absent", lambda: None)
    monkeypatch.setattr(service_stop, "live_terminals", lambda: ["ava-agent-1-shell-9"])

    def selector_moved(*_args: object, **_kwargs: object) -> None:
        pytest.fail("selector moved")

    monkeypatch.setattr("cli.release_transition.local.activate_release", selector_moved)
    with pytest.raises(RuntimeError, match="terminals appeared after release closure"):
        transition.select(read_operation(request_record.path))
