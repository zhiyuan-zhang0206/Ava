"""Fleet graph PG degradation through a router with explicit app-owned resources."""

from collections.abc import Iterator
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import errors as pg_errors

from base import telemetry
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.events.live.tests.fakes import patch_sync_redis
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
    redis.get.return_value = None
    patch_sync_redis(monkeypatch, lambda: redis)
    app = FastAPI()
    app.include_router(fg.router)
    app.state.bus = event_bus
    app.state.fleet_graph_stale_emitter = fg.FleetGraphStaleEmitter()
    app.state.telemetry_staleness = TelemetryStaleness(
        check_interval_s=0, read_heartbeat_age=_fresh_heartbeat_age
    )
    with database.pool(max_size=2) as pool:
        app.state.db_pool = pool
        yield app


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def capture_emit(
        _category: str,
        event_name: str,
        *,
        attributes: dict[str, Any] | None = None,
        **_kwargs: object,
    ) -> None:
        if event_name == "fleet_graph_stale":
            assert attributes is not None
            captured.append(dict(attributes))

    monkeypatch.setattr(telemetry, "emit", capture_emit)
    return captured


def _get_stale(app: FastAPI) -> tuple[int, bool]:
    with TestClient(app) as client:
        response = client.get("/api/fleet/graph")
    return response.status_code, bool(response.json()["stale"])


def test_pg_timeout_emits(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    """PG statement timeout (QueryCanceled) emits the pg_timeout reason."""

    pool = MagicMock()
    pool.connection.side_effect = pg_errors.QueryCanceled(
        "canceling statement due to statement timeout"
    )
    monkeypatch.setattr(app.state, "db_pool", pool)
    assert _get_stale(app) == (200, True)
    assert emitted == [{"route": "fleet_graph", "reason": "pg_timeout"}]


def test_pg_phase_budget_emits(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    """PG phase crossing its deadline emits the pg_budget reason."""

    monotonic = iter((0.0, fg._ROUTE_TIMEOUT_S + 0.1))
    monkeypatch.setattr(fg, "_monotonic", lambda: next(monotonic))
    assert _get_stale(app) == (200, True)
    assert emitted == [{"route": "fleet_graph", "reason": "pg_budget"}]


def test_pg_phase_exceeding_route_budget_serves_stale_before_telemetry(
    app: FastAPI, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real DB phase beyond the budget returns stale before reading telemetry."""
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
        (agent_id,),
    )
    db_conn.commit()
    monotonic = iter((0.0, fg._ROUTE_TIMEOUT_S + 0.1))
    monkeypatch.setattr(fg, "_monotonic", lambda: next(monotonic))
    assert _get_stale(app) == (200, True)
