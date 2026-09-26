"""Journal rules, owner dispatch and the stage guard for the macOS helper root start."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import JsonValue, ValidationError

from cli.release_transition import journal, native, root_macos, root_service, stage
from cli.release_transition.journal import Journal, Operation, Phase
from cli.release_transition.launchd_custody import Birth, RootCustody
from cli.release_transition.local import LocalTransition
from cli.release_transition.request import Request
from shared.native_process import ownership
from shared.native_process.ownership import OwnedProcess
from tests.lifecycle.transition.macos.launchd_fake import EXECUTOR_BIRTH, Harness
from tests.lifecycle.transition.macos.launchd_fake import harness as harness

HELPER = Birth(pid=700, birth=7.5, starttime=None)
OTHER_HELPER = Birth(pid=702, birth=12.5, starttime=None)
ROOT = Birth(pid=710, birth=8.5, starttime=None)


def _custody(**changes: Any) -> dict[str, JsonValue]:
    record = RootCustody(direction="candidate", helper=HELPER, restarts=0)
    return record.model_copy(update=changes).model_dump(mode="json")


def _advance(harness: Harness, *phases: Phase) -> None:
    with journal.exclusive(harness.path) as current:
        for phase in phases:
            current.advance(phase)


def _starting(harness: Harness) -> None:
    harness.launched()
    _advance(harness, "quiescing", "stopping", "selecting", "starting")


def _root(harness: Harness) -> RootCustody | None:
    record = journal.read_operation(harness.path).root
    return None if record is None else RootCustody.model_validate(record)


# Journal: intent before effect, receipt bound to the intent.


def test_root_custody_is_journaled_only_while_a_launched_macos_release_starts(
    harness: Harness,
) -> None:
    _advance(harness, "quiescing", "stopping", "selecting", "starting")
    with (
        journal.exclusive(harness.path) as current,
        pytest.raises(ValueError, match="only while a macOS release starts"),
    ):
        current.root_intent(_custody())  # dispatch was never attempted
    assert journal.read_operation(harness.path).phase == "starting" and _root(harness) is None


def test_root_custody_refuses_outside_the_starting_phase(harness: Harness) -> None:
    harness.launched()
    _advance(harness, "quiescing", "stopping")
    with journal.exclusive(harness.path) as current:
        with pytest.raises(ValueError, match="only while a macOS release starts"):
            current.root_intent(_custody())
        with pytest.raises(ValueError, match="only while a macOS release starts"):
            current.root_started(_custody(root=ROOT))


def test_root_intent_precedes_its_receipt_for_the_current_direction(harness: Harness) -> None:
    _starting(harness)
    with journal.exclusive(harness.path) as current:
        with pytest.raises(ValueError, match="must precede its receipt"):
            current.root_intent(_custody(root=ROOT))
        with pytest.raises(ValueError, match="must precede its receipt"):
            current.root_intent(_custody(direction="previous"))
        with pytest.raises(ValueError, match="requires its journaled start intent"):
            current.root_started(_custody(root=ROOT))
        current.root_intent(_custody())
        revision = current.operation.revision
        # Same helper, later baseline: the earliest baseline is kept.
        assert current.root_intent(_custody(restarts=3)).revision == revision
    assert _root(harness) == RootCustody.model_validate(_custody())


@pytest.mark.parametrize(
    "changed",
    [{"restarts": 1}, {"helper": OTHER_HELPER}, {"direction": "previous"}],
)
def test_root_receipt_must_match_its_intent(harness: Harness, changed: dict[str, Any]) -> None:
    _starting(harness)
    with journal.exclusive(harness.path) as current:
        current.root_intent(_custody())
        with pytest.raises(ValueError, match="differs from its intent"):
            current.root_started(_custody(root=ROOT, **changed))
        current.root_started(_custody(root=ROOT))
        revision = current.operation.revision
        assert current.root_started(_custody(root=ROOT)).revision == revision
        with pytest.raises(ValueError, match="cannot change"):
            current.root_started(_custody(root=ROOT.model_copy(update={"pid": 711})))
        with pytest.raises(ValueError, match="verified, never replaced"):
            current.root_intent(_custody())
        with pytest.raises(ValueError, match="verified, never replaced"):
            current.root_intent(_custody(helper=OTHER_HELPER))


def test_new_helper_or_direction_replaces_an_unreceipted_intent(harness: Harness) -> None:
    _starting(harness)
    with journal.exclusive(harness.path) as current:
        current.root_intent(_custody())
        current.root_intent(_custody(helper=OTHER_HELPER, restarts=0))
    assert _root(harness) == RootCustody.model_validate(_custody(helper=OTHER_HELPER))
    with journal.exclusive(harness.path) as current:
        current.root_started(_custody(helper=OTHER_HELPER, root=ROOT))
        current.recover("candidate failed")
        for phase in ("selecting", "starting"):
            current.advance(phase)
        # The recovery direction starts over; the candidate receipt is history.
        current.root_intent(_custody(direction="previous"))
    assert _root(harness) == RootCustody.model_validate(_custody(direction="previous"))


def test_operation_rejects_root_custody_outside_a_macos_release(harness: Harness) -> None:
    operation = journal.read_operation(harness.path)
    linux = operation.model_dump(mode="json") | {
        "launch": {"kind": native.LINUX},
        "root": _custody(),
    }
    with pytest.raises(ValidationError, match="only to a macOS release"):
        Operation.model_validate_json(json.dumps(linux))
    malformed = operation.model_dump(mode="json") | {"root": {"direction": "candidate"}}
    with pytest.raises(ValidationError, match="helper"):
        Operation.model_validate_json(json.dumps(malformed))
    # The same custody on the darwin release validates (control).
    darwin = operation.model_dump(mode="json") | {"root": _custody()}
    assert Operation.model_validate_json(json.dumps(darwin)).root == _custody()


# Dispatch: the root owner follows the recorded executor kind.


def test_root_owner_follows_the_recorded_executor_kind() -> None:
    assert native.helper_root({"kind": native.DARWIN}) is True
    assert native.helper_root({"kind": native.LINUX}) is False
    # No recorded launch keeps the Linux boot-unit contract, which refuses off Linux.
    assert native.helper_root(None) is False
    with pytest.raises(ValueError, match="no adapter kind"):
        native.helper_root({"unit": "unlabelled"})


def _transition(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> LocalTransition:
    from shared import maintenance

    request = journal.read_operation(harness.path).request
    assert isinstance(request, Request)
    transition = object.__new__(LocalTransition)
    transition.request, transition.home = request, Path(request.home)
    transition.previous = transition.candidate = SimpleNamespace(name="image")  # type: ignore[assignment]
    monkeypatch.setattr(Request, "require_configuration", lambda _self: None)
    monkeypatch.setattr(transition, "preflight", lambda: None)
    hold = SimpleNamespace(phase="stopped")
    monkeypatch.setattr(
        maintenance, "require_operation", lambda *_args: SimpleNamespace(maintenance=hold)
    )
    monkeypatch.setattr(maintenance, "set_phase", lambda *_args: None)
    return transition


@pytest.mark.parametrize("kind", [native.DARWIN, native.LINUX])
def test_start_observe_and_steady_state_dispatch_by_recorded_kind(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    transition = _transition(harness, monkeypatch)
    calls: list[str] = []

    def record(name: str) -> Any:
        def call(*args: object) -> None:
            calls.append(name)
            if name == "helper.start":
                assert isinstance(args[0], Journal), "the helper start journals its custody"

        return call

    for owner, prefix in ((root_macos, "helper"), (root_service, "unit")):
        for name in ("start", "observe", "restore_boot"):
            monkeypatch.setattr(owner, name, record(f"{prefix}.{name}"))
    operation = journal.read_operation(harness.path).model_copy(
        update={"launch": {"kind": kind}, "launch_attempted": True}
    )
    transition.start(Journal(operation))
    transition.observe(operation)
    prefix = "helper" if kind == native.DARWIN else "unit"
    assert calls == [f"{prefix}.start", f"{prefix}.observe", f"{prefix}.restore_boot"]


@pytest.mark.parametrize("kind", [native.DARWIN, native.LINUX])
def test_stop_authenticates_the_helper_before_and_proves_its_stop_intent_after(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from cli.commands import _maintenance, _root_driver
    from shared import maintenance

    transition = _transition(harness, monkeypatch)
    hold = SimpleNamespace(phase="drained")
    monkeypatch.setattr(
        maintenance, "require_operation", lambda *_args: SimpleNamespace(maintenance=hold)
    )
    events: list[str] = []
    monkeypatch.setattr(root_macos, "verified_helper", lambda _op: events.append("authenticate"))
    monkeypatch.setattr(_maintenance, "_stop", lambda *_args, **_kwargs: events.append("stop"))
    monkeypatch.setattr(_root_driver, "_require_root_absent", lambda: events.append("absent"))
    monkeypatch.setattr(root_macos, "require_stopped", lambda _op: events.append("stop-intent"))
    operation = journal.read_operation(harness.path).model_copy(update={"launch": {"kind": kind}})
    transition.stop(operation)
    assert events == (
        ["authenticate", "stop", "absent", "stop-intent"]
        if kind == native.DARWIN
        else ["stop", "absent"]
    )


# Stage: only the recorded executor may run the macOS start action.


def _darwin_operation(harness: Harness) -> Operation:
    harness.record_native(harness.launched())
    return journal.read_operation(harness.path)


def test_macos_start_action_must_be_a_finite_tool_of_the_recorded_executor(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared import os_boot_unit

    operation = _darwin_operation(harness)
    home = Path(operation.request.home)
    monkeypatch.setattr(stage, "sys", SimpleNamespace(platform="darwin"))
    # The birth comparison reads its own platform: tick-less darwin births.
    monkeypatch.setattr(ownership, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(os_boot_unit, "in_boot_unit", lambda _home: pytest.fail("not Linux"))
    parent = {"birth": EXECUTOR_BIRTH}
    monkeypatch.setattr(OwnedProcess, "capture", staticmethod(lambda _process: parent["birth"]))
    stage._require_native_root_owner(operation, home)
    parent["birth"] = OwnedProcess(EXECUTOR_BIRTH.pid, 99.0, None)
    with pytest.raises(RuntimeError, match="finite tool of the recorded executor"):
        stage._require_native_root_owner(operation, home)
    parent["birth"] = EXECUTOR_BIRTH
    harness.alive.discard(EXECUTOR_BIRTH)
    with pytest.raises(RuntimeError, match="finite tool of the recorded executor"):
        stage._require_native_root_owner(operation, home)
    with pytest.raises(RuntimeError, match="recorded executor receipt"):
        stage._require_native_root_owner(operation.model_copy(update={"native": None}), home)
    monkeypatch.setattr(stage, "sys", SimpleNamespace(platform="linux"))
    with pytest.raises(RuntimeError, match="must run on macOS"):
        stage._require_native_root_owner(operation, home)


def test_linux_start_action_still_requires_the_boot_unit(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared import os_boot_unit

    operation = journal.read_operation(harness.path).model_copy(
        update={"launch": {"kind": native.LINUX}}
    )
    monkeypatch.setattr(os_boot_unit, "in_boot_unit", lambda _home: False)
    with pytest.raises(RuntimeError, match="inside the ordinary root boot unit"):
        stage._require_native_root_owner(operation, Path(operation.request.home))


def test_macos_observation_has_no_boot_unit_but_must_run_on_macos(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared import os_boot_unit

    operation = _darwin_operation(harness)
    monkeypatch.setattr(os_boot_unit, "_manager_properties", lambda _home: pytest.fail("systemd"))
    monkeypatch.setattr(stage, "sys", SimpleNamespace(platform="darwin"))
    stage._require_root_owned(operation, Path(operation.request.home))
    monkeypatch.setattr(stage, "sys", SimpleNamespace(platform="linux"))
    with pytest.raises(RuntimeError, match="must run on macOS"):
        stage._require_root_owned(operation, Path(operation.request.home))
