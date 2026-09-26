"""Read-only home reservation admission before a release can interrupt work."""

from pathlib import Path

from cli.release_transition.request import HomeRequest
from cli.start_identity import read_intent
from shared import cluster


def require_reservation(request: HomeRequest, *, active_registry: Path) -> None:
    registry = Path(request.registry)
    if registry.resolve(strict=True) != registry or registry != active_registry.resolve(
        strict=True
    ):
        raise ValueError("release request does not name the active registry authority")
    intent = read_intent(Path(request.home))
    if (
        intent is None
        or intent["phase"] not in {"provisioned", "ready"}
        or intent["record"] is None
    ):
        raise ValueError("release requires an initialized gateway reservation")
    records = cluster.load_registry(path=registry)
    if request.home not in records or records[request.home] != cluster.ClusterRecord(
        **intent["record"]
    ):
        raise ValueError("release home reservation differs from persisted start identity")


def require_local_writers(request: HomeRequest) -> None:
    """Current one-host writer boundary; native resource/fleet fencing extends it."""
    import os

    from cli.commands._maintenance_stop import require_no_terminals
    from shared.cluster import registry_path
    from shared.config import settings
    from shared.db import connect
    from shared.machine import machine_name, machine_role

    if os.environ["AVA_HOME"] != request.home or machine_name() != request.machine:
        raise ValueError("loaded unit differs from home operation")
    require_reservation(request, active_registry=registry_path())
    if "gateway" not in machine_role() or settings.data_plane.is_remote:
        raise ValueError("this operation requires one local gateway data plane")
    if settings.data_plane.cluster_secret or settings.data_plane.data_plane_host:
        raise ValueError("networked clusters require the fleet writer barrier")
    with connect() as conn:
        units = set(conn.execute("SELECT machine_name, home FROM machine_units").fetchall())
        machines = {row[0] for row in conn.execute("SELECT name FROM machines").fetchall()}
    if units != {(request.machine, request.home)} or machines != {request.machine}:
        raise ValueError("all registered units must belong to this one-host operation")
    require_no_terminals()
