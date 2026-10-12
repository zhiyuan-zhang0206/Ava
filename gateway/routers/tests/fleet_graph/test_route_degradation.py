"""Fleet graph PG failures through a router with explicit app-owned resources."""

from collections.abc import Iterator
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import errors as pg_errors

from base.db import Database
from base.events.live.bus import EventBus
from base.events.live.tests.fakes import patch_sync_redis
from gateway.http.middleware.error_handlers import unhandled_exception_handler
from gateway.lgtm.telemetry_staleness import TelemetryStaleness
from gateway.routers import fleet_graph as fg


def _fresh_heartbeat_age(pool: object, *, now: datetime) -> float:
    del pool, now
    return 30.0


@pytest.fixture
def app(
    database: Database, event_bus: EventBus, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FastAPI]:
    """Own the real PG pool without importing or starting unrelated gateway services."""
    redis = MagicMock()
    redis.__enter__.return_value = redis
    # A long-lived entry from an earlier shape of the cache must never be served.
    leftover = fg.FleetGraphResponse(nodes=[], edges=[]).model_dump_json()

    def get(key: str) -> str | None:
        return leftover if "last_good" in key else None

    redis.get.side_effect = get
    patch_sync_redis(monkeypatch, lambda: redis)
    app = FastAPI()
    app.include_router(fg.router)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.state.bus = event_bus
    app.state.telemetry_staleness = TelemetryStaleness(
        check_interval_s=0, read_heartbeat_age=_fresh_heartbeat_age
    )
    with database.pool(max_size=2) as pool:
        app.state.db_pool = pool
        yield app


@pytest.mark.parametrize(
    "failure",
    [
        pg_errors.QueryCanceled("canceling statement due to statement timeout"),
        pg_errors.OperationalError("connection refused"),
    ],
)
def test_pg_failure_is_a_typed_500_not_a_fallback_graph(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """Neither a statement timeout nor another PG failure is answered with a graph."""
    pool = MagicMock()
    pool.connection.side_effect = failure
    monkeypatch.setattr(app.state, "db_pool", pool)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/fleet/graph")

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "nodes" not in response.json()
