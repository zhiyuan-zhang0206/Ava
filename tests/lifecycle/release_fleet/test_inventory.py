"""The fleet inventory gate: every registered writer is accounted for before any effect.

`check_inventory` is pure. `require_fleet_of_one` / `require_topology` read the
loaded identity, Settings and the `machine_units` / `machines` rows; those
reads are replaced here (the rows are real on PostgreSQL in
tests/lifecycle/db_authority/test_fleet_of_one.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cli.release_fleet import inventory
from cli.release_fleet.inventory import (
    NETWORKED_REFUSAL,
    check_inventory,
    require_fleet_of_one,
    require_topology,
)
from cli.release_fleet.policy import UnitKey
from cli.release_fleet.request import CoordinatorEndpoint, Exclusion, FleetRequest
from tests.lifecycle.release_fleet.test_models import _request, _spec
from tests.lifecycle.transition.test_identity import prepared as prepared

_RUNNER = UnitKey(machine="macbook-air", home="/Users/zzy/.ava")
_ENDPOINT = CoordinatorEndpoint(host="10.0.0.5", port=8121)


def _live_terminal() -> None:
    raise RuntimeError("persistent terminals/schedules ... will not kill or replay them")


def test_every_registered_unit_is_the_gateway_included_or_excluded() -> None:
    request = _request(units=(_spec(_RUNNER),), coordinator=_ENDPOINT)
    gateway = ("ubuntu", "/home/zzy/.ava")
    check_inventory(request, {gateway, _RUNNER.order}, paused=())
    with pytest.raises(ValueError, match="neither included nor excluded"):
        check_inventory(request, {gateway, _RUNNER.order, ("win", "C:\\ava")}, paused=())
    with pytest.raises(ValueError, match="not registered"):
        check_inventory(request, {gateway}, paused=())
    with pytest.raises(ValueError, match="on a paused machine; exclude it"):
        check_inventory(request, {gateway, _RUNNER.order}, paused={"macbook-air"})
    with pytest.raises(ValueError, match="gateway machine ubuntu is paused"):
        check_inventory(request, {gateway, _RUNNER.order}, paused={"ubuntu"})
    excluded = _request(excluded=(Exclusion(unit=_RUNNER, reason="paused", recorded_by="request"),))
    check_inventory(excluded, {gateway, _RUNNER.order}, paused={"macbook-air"})


@pytest.fixture
def lone_gateway(prepared: FleetRequest, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The loaded identity of a single box, and the rows its database holds."""
    import shared.cluster
    import shared.machine
    import shared.paths
    from cli.commands import maintenance_stop
    from shared.config import settings

    rows: dict[str, Any] = {
        "units": {("unit", prepared.home)},
        "machines": {"unit"},
        "paused": set(),
    }
    monkeypatch.setattr(shared.paths, "ava_home", lambda: Path(prepared.home))
    monkeypatch.setattr(shared.machine, "machine_name", lambda: "unit")
    monkeypatch.setattr(shared.machine, "machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(shared.cluster, "registry_path", lambda: Path(prepared.registry))
    monkeypatch.setattr(type(settings.data_plane), "is_remote", property(lambda _self: False))
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    monkeypatch.setattr(settings.data_plane, "data_plane_host", "")
    monkeypatch.setattr(
        inventory,
        "registered_units",
        lambda: (set(rows["units"]), set(rows["machines"]), set(rows["paused"])),
    )
    # Live terminals and schedules are the stop phase's to close, never a refusal.
    monkeypatch.setattr(maintenance_stop, "require_no_terminals", _live_terminal)
    return rows


def test_the_lone_gateway_home_is_a_fleet_of_one(
    prepared: FleetRequest, lone_gateway: dict[str, Any]
) -> None:
    require_topology(prepared)
    require_fleet_of_one(Path(prepared.home))  # PITR's form: the home alone


def test_a_networked_cluster_refuses_naming_the_missing_slices(
    prepared: FleetRequest, lone_gateway: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.data_plane, "cluster_secret", "bearer")
    with pytest.raises(ValueError, match="dbgen-8") as refused:
        require_fleet_of_one(Path(prepared.home))
    assert str(refused.value) == NETWORKED_REFUSAL and "FC-9" in NETWORKED_REFUSAL


@pytest.mark.parametrize("extra", ["unit", "machine"])
def test_any_other_registered_writer_refuses(
    prepared: FleetRequest, lone_gateway: dict[str, Any], extra: str
) -> None:
    if extra == "unit":
        lone_gateway["units"].add(("macbook-air", "/Users/zzy/.ava"))
    else:
        lone_gateway["machines"].add("macbook-air")
    with pytest.raises(ValueError, match="every registered unit must be this home"):
        require_topology(prepared)


def test_the_loaded_identity_must_be_the_operations(
    prepared: FleetRequest, lone_gateway: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import shared.machine

    with pytest.raises(ValueError, match="loaded home differs"):
        require_fleet_of_one(Path(prepared.home).with_name("other"))
    monkeypatch.setattr(shared.machine, "machine_role", lambda: frozenset({"agent-runner"}))
    with pytest.raises(ValueError, match="one local gateway data plane"):
        require_fleet_of_one(Path(prepared.home))
    monkeypatch.setattr(shared.machine, "machine_name", lambda: "another")
    with pytest.raises(ValueError, match="loaded machine differs"):
        require_topology(prepared)


def test_a_request_naming_other_units_is_inventoried_then_refused(
    prepared: FleetRequest, lone_gateway: dict[str, Any]
) -> None:
    exclusion = Exclusion(unit=_RUNNER, reason="offline", recorded_by="operator")
    request = prepared.model_copy(update={"excluded": (exclusion,)})
    FleetRequest.model_validate(request.model_dump())
    # An unregistered exclusion is named precisely before the dbgen-8 boundary.
    with pytest.raises(ValueError, match="not registered"):
        require_topology(request)
    lone_gateway["units"].add(_RUNNER.order)
    with pytest.raises(ValueError, match="dbgen-8"):
        require_topology(request)


def test_a_remote_unit_request_always_refuses(prepared: FleetRequest) -> None:
    request = _request(units=(_spec(_RUNNER),), coordinator=_ENDPOINT)
    with pytest.raises(ValueError, match="dbgen-8"):
        require_topology(request.unit_request(_RUNNER))
