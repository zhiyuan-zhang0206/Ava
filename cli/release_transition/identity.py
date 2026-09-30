"""Read-only home reservation admission before a release can interrupt work."""

from pathlib import Path

from cli.release_transition.request import HomeRequest
from cli.start_identity import read_intent


def require_reservation(request: HomeRequest) -> None:
    """The home must be an initialized gateway: its own start intent carries its record."""
    intent = read_intent(Path(request.home))
    if (
        intent is None
        or intent["phase"] not in {"provisioned", "ready"}
        or intent["record"] is None
    ):
        raise ValueError("release requires an initialized gateway reservation")
