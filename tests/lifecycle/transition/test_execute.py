"""Durable phase ordering across candidate failure and executor interruption."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from cli.release_transition.execute import drive
from cli.release_transition.journal import (
    Direction,
    Journal,
    Operation,
    create,
    exclusive,
    read_operation,
)
from cli.release_transition.local import LocalTransition
from cli.release_transition.request import ReleaseRef, Request


@pytest.fixture
def request_record(tmp_path: Path) -> Request:
    home = tmp_path.resolve() / "home"
    home.mkdir()
    previous = ReleaseRef(
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        schema_digest="c" * 64,
        source_commit="d" * 40,
    )
    candidate = previous.model_copy(update={"artifact_digest": "e" * 64})
    (home / "releases").mkdir()
    (home / "releases/current-release").write_text(
        json.dumps(
            {
                "artifact_digest": previous.artifact_digest,
                "manifest_digest": previous.manifest_digest,
            }
        )
    )
    return Request(
        id=uuid4(),
        home=str(home),
        registry=str(home.parent / "registry.json"),
        created_at=datetime.now(UTC),
        platform_tag="test",
        machine="test",
        previous=previous,
        candidate=candidate,
        executor=candidate,
        configuration_digest="f" * 64,
    )


class ControllerLost(BaseException):
    """A process death cannot run the executor's exception compensation."""


class Effects(LocalTransition):
    def __init__(self, request: Request, *, fail: str = "", crash: bool = False) -> None:
        self.request = request
        self.events: list[tuple[str, str]] = []
        self.fail = fail
        self.crash = crash
        self.selector = "previous"
        self.selector_writes = 0

    def _effect(self, phase: str) -> Operation:
        operation = read_operation(self.request.path)
        assert operation.phase == phase, "intent must be durable before an effect"
        self.events.append((phase, operation.direction))
        if self.fail == phase:
            self.fail = ""
            if self.crash:
                raise ControllerLost
            raise RuntimeError("injected native failure")
        return operation

    def preflight(self) -> None:
        self._effect("prepared")

    def quiesce(self) -> None:
        self._effect("quiescing")

    def stop(self, operation: Operation) -> None:
        assert operation == self._effect("stopping")

    def select(self, operation: Operation) -> None:
        if self.selector != operation.direction:
            self.selector = operation.direction
            self.selector_writes += 1
        assert operation == self._effect("selecting")

    def start(self, operation: Operation) -> None:
        assert operation == self._effect("starting")

    def observe(self, operation: Operation) -> None:
        assert operation == self._effect("observing")

    def resume(self, operation: Operation) -> None:
        assert operation == self._effect("resuming")


def test_candidate_start_failure_closes_candidate_before_selecting_previous(
    request_record: Request,
) -> None:
    create(request_record)
    effects = Effects(request_record, fail="starting")
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    assert effects.events == [
        ("prepared", "candidate"),
        ("quiescing", "candidate"),
        ("stopping", "candidate"),
        ("selecting", "candidate"),
        ("starting", "candidate"),
        ("stopping", "previous"),
        ("selecting", "previous"),
        ("starting", "previous"),
        ("observing", "previous"),
        ("resuming", "previous"),
    ]
    final = read_operation(request_record.path)
    assert final.terminal and final.direction == "previous"
    assert effects.selector == "previous"


@pytest.mark.parametrize("phase", ["quiescing", "stopping", "selecting", "resuming"])
def test_uncertain_effect_keeps_phase_and_never_invents_rollback(
    request_record: Request, phase: str
) -> None:
    create(request_record)
    effects = Effects(request_record, fail=phase)
    with (
        pytest.raises(RuntimeError, match="native failure"),
        exclusive(request_record.path) as journal,
    ):
        drive(journal, effects)
    interrupted = read_operation(request_record.path)
    assert interrupted.phase == phase and interrupted.direction == "candidate"
    assert not interrupted.terminal and interrupted.error is not None
    assert all(direction == "candidate" for _, direction in effects.events)
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    assert read_operation(request_record.path).terminal
    assert effects.selector_writes == 1


@pytest.mark.parametrize("phase", ["stopping", "selecting", "starting", "observing", "resuming"])
def test_process_death_retains_exact_decision_for_reconciliation(
    request_record: Request, phase: str
) -> None:
    create(request_record)
    effects = Effects(request_record, fail=phase, crash=True)
    with pytest.raises(ControllerLost), exclusive(request_record.path) as journal:
        drive(journal, effects)
    interrupted = read_operation(request_record.path)
    assert interrupted.phase == phase and interrupted.error is None
    with exclusive(request_record.path) as journal:
        drive(journal, effects)
    assert read_operation(request_record.path).terminal
    assert effects.selector_writes == 1


def test_failed_recovery_remains_held_instead_of_looping_between_releases(
    request_record: Request,
) -> None:
    create(request_record)
    effects = Effects(request_record, fail="starting")
    with exclusive(request_record.path) as journal:
        journal.advance("quiescing")
        journal.advance("stopping")
        journal.advance("selecting")
        journal.advance("starting")
        journal.recover("candidate failed")
        with pytest.raises(RuntimeError, match="native failure"):
            drive(journal, effects)
    state = read_operation(request_record.path)
    assert state.direction == "previous" and state.phase == "starting" and not state.terminal
    assert all(direction == "previous" for _, direction in effects.events)


def _reach_resuming(journal: Journal, direction: Direction) -> None:
    for phase in ("quiescing", "stopping", "selecting", "starting"):
        journal.advance(phase)
    if direction == "previous":
        journal.recover("candidate failed")
        journal.advance("selecting")
        journal.advance("starting")
    journal.advance("observing")
    journal.advance("resuming")


@pytest.mark.parametrize("changed", [False, True])
def test_quiescing_rechecks_predecessor_after_readonly_preflights_before_any_disruption(
    request_record: Request, monkeypatch: pytest.MonkeyPatch, *, changed: bool
) -> None:
    from cli.release_transition import root_service
    from ops import agent_pause
    from shared.runtime_release import VerifiedRelease

    create(request_record)
    home = Path(request_record.home)
    transition = object.__new__(LocalTransition)
    transition.request, transition.home = request_record, home
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

    monkeypatch.setattr(transition, "preflight", lambda: None)
    monkeypatch.setattr(root_service, "preflight", preflight)
    monkeypatch.setattr(agent_pause, "_prepare", effect("prepare"))
    monkeypatch.setattr(agent_pause, "_drain", effect("drain"))
    monkeypatch.setattr(transition, "stop", stop)
    message = "predecessor is not the selected" if changed else "positive control reached stop"
    with (
        pytest.raises((ValueError, RuntimeError), match=message),
        exclusive(request_record.path) as handle,
    ):
        drive(handle, transition)
    assert observations == [True, False]
    assert effects == ([] if changed else ["prepare", "drain", "stop"])
    final = read_operation(request_record.path)
    assert final.phase == ("quiescing" if changed else "stopping")
    assert final.direction == "candidate" and not final.terminal and final.error is not None


@pytest.mark.parametrize("direction", ["candidate", "previous"])
@pytest.mark.parametrize("pause_status", ["held", "resumed"])
@pytest.mark.parametrize("healthy", [True, False])
def test_resuming_reobserves_selected_root_before_admission_or_completion(
    request_record: Request,
    monkeypatch: pytest.MonkeyPatch,
    direction: Direction,
    pause_status: str,
    healthy: bool,
) -> None:
    from cli.commands import _maintenance
    from cli.release_transition import root_service
    from shared import maintenance, pause_owner, start_serving
    from shared.runtime_release import VerifiedRelease
    from shared.start_inputs import configuration_digest

    home = Path(request_record.home)
    request = request_record.model_copy(update={"configuration_digest": configuration_digest(home)})
    create(request)
    transition = object.__new__(LocalTransition)
    transition.request = request
    transition.home = home
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

    monkeypatch.setattr(root_service, "observe", observe)
    monkeypatch.setattr(pause_owner, "read", read_pause)
    monkeypatch.setattr(maintenance, "require_operation", require_holder)
    monkeypatch.setattr(_maintenance, "_resume", resume)
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
