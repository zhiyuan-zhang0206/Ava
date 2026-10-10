"""Machine rows, probe responses and pool lifetime for heartbeat liveness tests."""

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from ops.cluster.rpc import ClusterOpUnreachable

MACHINE = "test-runner-1"


@pytest.fixture
def pool():
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


def register_machine(db: psycopg.Connection, name: str = MACHINE) -> None:
    """Register an agent-runner machine row (probe target)."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO machines (name, gateway_url, role) VALUES (%s, %s, %s) "
            "ON CONFLICT (name) DO NOTHING",
            (name, "http://127.0.0.1:1", "{agent-runner}"),
        )
    db.commit()


class FakeProbe:
    """Injectable probe: returns per-machine reachability."""

    def __init__(self, reachable: dict[str, bool]) -> None:
        self.reachable = reachable
        self.calls: list[str] = []

    async def __call__(self, target_machine: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(target_machine)
        if not self.reachable.get(target_machine, True):
            raise ClusterOpUnreachable("unreachable")
        return {"status": "completed", "result": {}}


__all__ = ["MACHINE", "FakeProbe", "pool", "register_machine"]
