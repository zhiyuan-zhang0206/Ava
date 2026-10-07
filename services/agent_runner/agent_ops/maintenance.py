"""Keep admitted ops and uncancelled worker futures visible during local stop."""

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from base.deploy.maintenance import admission as maintenance_admission
from base.deploy.maintenance.state import MaintenancePhase

RequestTokens = set[object]
WorkerFutures = set[asyncio.Future[Any]]


@contextmanager
def admission(kind: str, *, requests: RequestTokens) -> Generator[None]:
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
    requests.add(token)
    try:
        yield
    finally:
        requests.remove(token)


def track_worker(future: asyncio.Future[Any], *, workers: WorkerFutures) -> None:
    workers.add(future)
    future.add_done_callback(workers.discard)


def progress(*, requests: RequestTokens, workers: WorkerFutures) -> dict[str, int]:
    return {"protocol": 1, "requests": len(requests), "workers": len(workers)}
