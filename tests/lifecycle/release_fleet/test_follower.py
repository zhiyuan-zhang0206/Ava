"""The unit follower's own rules, and the coordinator's ops transport.

The follower acts only on instructions for its own operation, unit and image,
never on an older one, and holds (raises) when its coordinator stays silent
past its lifetime. The ops transport runs the frozen v1 `release_image_exec`
at the URL the unit's row advertises.
"""

from __future__ import annotations

import base64
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cli.release_fleet.client import CoordinatorAwayError
from cli.release_fleet.follower import Follower
from cli.release_fleet.progress import Instruction
from cli.release_fleet.units import OpsTransport
from cli.release_transition.journal import create, exclusive, read_operation
from tests.lifecycle.release_fleet.fakes import Clock
from tests.lifecycle.release_fleet.remote import Exchange, UnitEffects, fleet_request


class Scripted:
    """A coordinator client that serves a fixed list of pulls and records answers."""

    def __init__(self, pulls: list[Instruction | Exception | None]) -> None:
        self.pulls = pulls
        self.reports: list[Any] = []

    def instruction(self) -> Instruction | None:
        pulled = self.pulls.pop(0) if self.pulls else None
        if isinstance(pulled, Exception):
            raise pulled
        return pulled

    def report(self, report: Any) -> None:
        self.reports.append(report)

    def capability(self) -> bytes:
        raise AssertionError("not asked in these tests")


def _unit(tmp_path: Path) -> tuple[Any, Any]:
    request = fleet_request(tmp_path.resolve())
    unit_request = request.unit_request(request.units[0].unit)
    create(unit_request)
    return request, unit_request


def _order(request: Any, unit_request: Any, **changes: Any) -> Instruction:
    fields: dict[str, Any] = {
        "operation": request.id,
        "unit": unit_request.unit,
        "sequence": 1,
        "action": "standby",
        "direction": "candidate",
        "image": unit_request.candidate.selector,
        "maintenance_at": unit_request.created_at,
    } | changes
    return Instruction.model_validate(fields)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("operation", "another operation or unit"),
        ("image", "another image"),
        ("maintenance", "another maintenance hold"),
    ],
)
def test_a_unit_refuses_an_instruction_that_is_not_its_own(
    tmp_path: Path, change: str, message: str
) -> None:
    from uuid import uuid4

    request, unit_request = _unit(tmp_path)
    changes: dict[str, Any] = {
        "operation": {"operation": uuid4()},
        "image": {"image": unit_request.previous.selector},
        "maintenance": {"maintenance_at": unit_request.created_at + timedelta(seconds=1)},
    }[change]
    effects = UnitEffects()
    with exclusive(unit_request.path) as journal:
        follower = Follower(journal, effects, Scripted([]), Exchange())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match=message):
            follower.follow(_order(request, unit_request, **changes))
    assert effects.events == []
    unit = read_operation(unit_request.path).unit
    assert unit is not None and unit.acted == ()


def test_an_older_instruction_is_never_acted_on_again(tmp_path: Path) -> None:
    request, unit_request = _unit(tmp_path)
    quiesce = _order(request, unit_request, sequence=2, action="quiesce")
    client = Scripted([])
    effects = UnitEffects()
    with exclusive(unit_request.path) as journal:
        follower = Follower(journal, effects, client, Exchange())  # type: ignore[arg-type]
        follower.follow(quiesce)
        follower.follow(_order(request, unit_request))  # sequence 1, replaced already
    assert effects.events == [("quiescing", "candidate")]
    assert [r.state for r in client.reports] == ["drained"]
    unit = read_operation(unit_request.path).unit
    assert unit is not None and unit.acted == (quiesce.digest,)


def test_an_answered_instruction_is_answered_again_without_repeating_its_effect(
    tmp_path: Path,
) -> None:
    request, unit_request = _unit(tmp_path)
    quiesce = _order(request, unit_request, action="quiesce")
    client = Scripted([])
    effects = UnitEffects()
    with exclusive(unit_request.path) as journal:
        follower = Follower(journal, effects, client, Exchange())  # type: ignore[arg-type]
        follower.follow(quiesce)
        follower.follow(quiesce)
    assert effects.events == [("quiescing", "candidate")]
    assert client.reports[0] == client.reports[1]


def test_a_silent_coordinator_past_the_lifetime_holds_the_unit(tmp_path: Path) -> None:
    request, unit_request = _unit(tmp_path)
    clock = Clock(unit_request.created_at)
    away = CoordinatorAwayError("listener closed")
    client = Scripted([_order(request, unit_request), away, away, away, away])
    with exclusive(unit_request.path) as journal:
        follower = Follower(
            journal,
            UnitEffects(),
            client,  # type: ignore[arg-type]
            Exchange(),
            clock=clock,
            sleep=clock.sleep,
            poll_s=60,
            lifetime_s=150,
        )
        with pytest.raises(RuntimeError, match="silent past this unit executor's lifetime"):
            follower.run()
    held = read_operation(unit_request.path)
    assert not held.terminal and held.phase == "prepared"
    assert [r.state for r in client.reports] == ["dispatched"]


def test_the_ops_transport_runs_the_frozen_handoff_at_the_advertised_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.release_fleet import units
    from ops import cluster_rpc

    request, unit_request = _unit(tmp_path)
    spec = request.units[0]
    calls: list[tuple[Any, ...]] = []

    async def dispatch(machine: str, kind: str, payload: dict[str, Any], **kwargs: Any) -> Any:
        calls.append((machine, kind, payload, kwargs))
        return {"entry": payload["entry"], "result": {"ready": True}}

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", dispatch)
    monkeypatch.setattr(units, "_ops_url", lambda unit: f"http://10.0.0.7:8106/{unit.machine}")
    document = unit_request.model_dump_json().encode()
    assert OpsTransport().run(spec, "preflight", document) == {"ready": True}
    ((machine, kind, payload, kwargs),) = calls
    assert (machine, kind) == (spec.unit.machine, "release_image_exec")
    assert payload["entry"] == "preflight"
    assert payload["image"]["artifact_digest"] == spec.candidate.artifact_digest
    assert base64.b64decode(payload["request"]) == document
    assert kwargs["ops_url"] == "http://10.0.0.7:8106/macbook-air" and kwargs["retries"] == 0
