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
