"""Keep admitted ops and uncancelled worker futures visible during local stop."""

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from shared import maintenance

_requests: set[object] = set()
_workers: set[asyncio.Future[Any]] = set()


@contextmanager
def admission() -> Generator[None]:
    current = maintenance.snapshot()
    if (
        current is not None
        and current.maintenance is not None
        and current.maintenance.phase in ("stopping", "stopped", "starting", "ready")
    ):
        raise RuntimeError("unit is stopping or held for explicit maintenance resume")
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
