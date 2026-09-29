"""The fleet inventory check: which writers an operation must account for.

Every registered unit (`machine_units`) is the request's gateway unit, one of
its included units, or one of its exclusions, exactly; a paused machine's
units are always excluded. An unknown or unaccounted unit refuses the request
before any effect. The check is read-only and runs at submission and again at
every coordinator phase that could interrupt work.

Remote units cannot take part yet: a networked fleet needs each unit to
receive the new write generation over the coordinator channel (slice dbgen-8),
and a unit left out of a release can only rejoin through a converge
operation (slice FC-9). Until both exist every admitted operation is a fleet
of one, and `require_fleet_of_one` is the whole topology gate.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from cli.release_fleet.request import FleetRequest, UnitRequest

NETWORKED_REFUSAL = (
    "networked fleet releases need per-unit write-generation delivery over the "
    "coordinator channel (slice dbgen-8), and a unit left out of a release rejoins "
    "only through a converge operation (slice FC-9); until both exist a release "
    "admits exactly one unit, the gateway home"
)


def check_inventory(
    request: FleetRequest,
    registered: Collection[tuple[str, str]],
    paused: Collection[str],
) -> None:
    """The request accounts for every registered unit, and for nothing else."""
    excluded = {entry.unit.order: entry for entry in request.excluded}
    listed = {request.gateway.order, *(spec.unit.order for spec in request.units), *excluded}
    if unknown := sorted(listed - set(registered)):
        raise ValueError(f"the request names units that are not registered: {unknown}")
    if missing := sorted(set(registered) - listed):
        raise ValueError(f"registered units are neither included nor excluded: {missing}")
    if request.machine in paused:
        raise ValueError(f"the gateway machine {request.machine} is paused")
    for spec in request.units:
        if spec.unit.machine in paused:
            raise ValueError(f"unit {spec.unit.label} is on a paused machine; exclude it")


def registered_units() -> tuple[set[tuple[str, str]], set[str], set[str]]:
    """The registered units `(machine, home)`, machines, and paused machines."""
    from shared.db import connect

    with connect() as conn:
        units = {
            (row[0], row[1])
            for row in conn.execute("SELECT machine_name, home FROM machine_units").fetchall()
        }
        machines = conn.execute("SELECT name, paused_at IS NOT NULL FROM machines").fetchall()
    return units, {row[0] for row in machines}, {row[0] for row in machines if row[1]}


def require_fleet_of_one(home: Path) -> None:
    """The loaded `home` is the cluster's only unit: its own gateway, on a local data plane.

    Takes only the home, so every gateway-home operation (a fleet request, a
    PITR request) can share one single-unit topology gate. Live terminals and
    schedules are not refused here: an operation's stop phase closes them.
    """
    from shared.config import settings
    from shared.machine import machine_name, machine_role
    from shared.paths import ava_home

    if ava_home() != home:
        raise ValueError("the loaded home differs from the operation's home")
    if "gateway" not in machine_role() or settings.data_plane.is_remote:
        raise ValueError("this operation requires one local gateway data plane")
    if settings.data_plane.cluster_secret or settings.data_plane.data_plane_host:
        raise ValueError(NETWORKED_REFUSAL)
    units, machines, _paused = registered_units()
    machine = machine_name()
    if units != {(machine, str(home))} or machines != {machine}:
        raise ValueError(f"every registered unit must be this home: {NETWORKED_REFUSAL}")


def require_topology(request: FleetRequest | UnitRequest) -> None:
    """The operation's inventory gate, re-checked before each interrupting phase.

    A request that names other units is first held to the full inventory rule,
    so its refusal names the precise problem before the dbgen-8 boundary.
    """
    from cli.release_transition.identity import require_reservation
    from shared.cluster import registry_path
    from shared.machine import machine_name

    if isinstance(request, UnitRequest):
        raise ValueError(NETWORKED_REFUSAL)  # noqa: TRY004 — a topology refusal, not a type error
    if machine_name() != request.machine:
        raise ValueError("the loaded machine differs from the operation's gateway unit")
    require_reservation(request, active_registry=registry_path())
    if request.units or request.excluded:
        registered, _machines, paused = registered_units()
        check_inventory(request, registered, paused)
        raise ValueError(NETWORKED_REFUSAL)
    require_fleet_of_one(Path(request.home))
