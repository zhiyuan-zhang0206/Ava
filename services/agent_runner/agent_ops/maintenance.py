"""Keep admitted ops and uncancelled worker futures visible during local stop."""

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from base.deploy.maintenance import admission as maintenance_admission
from base.deploy.maintenance.state import MaintenancePhase

_requests: set[object] = set()
_workers: set[asyncio.Future[Any]] = set()


@contextmanager
def admission(kind: str) -> Generator[None]:
    """Keep readiness reachable during a hold; it stays counted until its work finishes."""
    current = maintenance_admission.snapshot()
    if (
        current is not None
        and kind != "status_probe"
        and current.maintenance is not None
        and current.maintenance.phase
        in (
            MaintenancePhase.STOPPING,
            MaintenancePhase.STOPPED,
            MaintenancePhase.STARTING,
            MaintenancePhase.READY,
        )
    ):
        raise RuntimeError("unit is stopping or held until `ava start` releases it")
    token = object()
    _requests.add(token)
    try:
        yield
    finally:
        _requests.remove(token)


def track_worker(future: asyncio.Future[Any]) -> None:
    _workers.add(future)
    future.add_done_callback(_workers.discard)


def progress() -> dict[str, int]:
    return {"protocol": 1, "requests": len(_requests), "workers": len(_workers)}
