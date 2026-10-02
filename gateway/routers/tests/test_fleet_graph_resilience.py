"""Regression tests for fleet-graph upstream failures and Redis fallbacks."""

from datetime import timedelta

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors as pg_errors

from base import telemetry
from base.agents import AgentStatus
from base.events.live.tests.fakes import patch_sync_redis
from gateway.app import app
from gateway.lgtm import prom_metrics, telemetry_staleness
from gateway.schemas.fleet_graph import FleetGraphNode, FleetGraphResponse


def _fresh_heartbeat_age(*, timeout_s: float | None = None) -> float:
    del timeout_s
    return 30.0


class _FakeRedis:
    """Minimal Redis graph-cache fake that records writes."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.writes: list[tuple[str, str, int | None]] = []

    def __enter__(self) -> "_FakeRedis":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self.values[key] = value
        self.writes.append((key, value, ex))


class _RedisFactory:
    """Callable replacement for the route's Redis context-manager factory."""

    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis

    def __call__(self, **_kwargs: object) -> _FakeRedis:
        return self._redis


@pytest.fixture(autouse=True)
def _fresh_telemetry_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing resilience cases isolate their named upstream failure."""
    monkeypatch.setattr(
        telemetry_staleness,
        "prometheus_heartbeat_age",
        _fresh_heartbeat_age,
    )
    monkeypatch.setattr(
        telemetry_staleness,
        "loki_heartbeat_age",
        _fresh_heartbeat_age,
    )
    monkeypatch.setattr(telemetry_staleness, "_source_states", {})
    monkeypatch.setattr(telemetry_staleness, "CHECK_INTERVAL_S", 0, raising=False)


def _seed_agent(db_conn: psycopg.Connection) -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        agent_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
            (agent_id,),
        )
    db_conn.commit()
    return agent_id


def _empty_prom(
    _metric: str,
    _by: str,
    *,
    window: timedelta | None = None,
    timeout_s: float | None = None,
) -> dict[str, float]:
    return {}


def _last_good_graph() -> FleetGraphResponse:
    return FleetGraphResponse(
        nodes=[
            FleetGraphNode(
                agent_id=999,
                label="last good",
                status=AgentStatus.RUNNING,
                liveness_state="online",
                spawner="test",
                machine=None,
                node_score=12.5,
                total_tokens=42,
            )
        ],
        edges=[],
    )


def test_success_writes_short_cache_and_last_good_graph(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full graph refresh updates both the poll cache and its last-good fallback."""
    _seed_agent(db_conn)
    redis = _FakeRedis()

    import gateway.routers.fleet_graph as fg

    patch_sync_redis(monkeypatch, _RedisFactory(redis))
    monkeypatch.setattr(prom_metrics, "sum_by", _empty_prom)
    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")

    assert resp.status_code == 200
    key = fg._cache_key(include_terminated=False, hours=None, decay_lambda=0.5)
    response_writes = redis.writes
    assert {write[0] for write in response_writes} == {key, f"fleet_graph:last_good:{key}"}
    assert redis.values[key] == redis.values[f"fleet_graph:last_good:{key}"]
    assert {write[0]: write[2] for write in response_writes} == {
        key: 60,
        f"fleet_graph:last_good:{key}": fg._LAST_GOOD_CACHE_TTL_SECONDS,
    }


def test_stale_heartbeat_marks_telemetry_and_keeps_fresh_graph_cached(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heartbeat outage does not turn a complete graph into stale data."""
    _seed_agent(db_conn)
    redis = _FakeRedis()

    import gateway.routers.fleet_graph as fg

    patch_sync_redis(monkeypatch, _RedisFactory(redis))
    monkeypatch.setattr(prom_metrics, "sum_by", _empty_prom)

    def missing_heartbeat(*, timeout_s: float | None = None) -> None:
        del timeout_s

    monkeypatch.setattr(telemetry_staleness, "prometheus_heartbeat_age", missing_heartbeat)
    emitted: list[tuple[str, dict[str, object]]] = []

    def capture_emit(
        _category: str,
        event_name: str,
        *,
        attributes: dict[str, object],
        **_kwargs: object,
    ) -> None:
        if event_name == "telemetry_read_stale":
            emitted.append((event_name, attributes))

    monkeypatch.setattr(telemetry, "emit", capture_emit)

    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")

    assert resp.status_code == 200
    body = resp.json()
    assert body["stale"] is False
    assert body["telemetry_stale"] is True
    assert body["snapshot_at"] is not None
    key = fg._cache_key(include_terminated=False, hours=None, decay_lambda=0.5)
    assert {write[0] for write in redis.writes} == {
        key,
        f"fleet_graph:last_good:{key}",
    }
    assert redis.values[key] == redis.values[f"fleet_graph:last_good:{key}"]
    assert emitted[0][0] == "telemetry_read_stale"
    assert emitted[0][1]["source"] == "prometheus"


def test_pg_phase_exceeding_route_budget_serves_stale_before_telemetry(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow DB phase does not start more upstream reads after its deadline."""
    _seed_agent(db_conn)
    redis = _FakeRedis()
    prom_calls = 0

    import gateway.routers.fleet_graph as fg

    monotonic = iter((0.0, fg._ROUTE_TIMEOUT_S + 0.1))
    patch_sync_redis(monkeypatch, _RedisFactory(redis))
    monkeypatch.setattr(fg, "_monotonic", lambda: next(monotonic))

    def prom(*_args: object, **_kwargs: object) -> dict[str, float]:
        nonlocal prom_calls
        prom_calls += 1
        return {}

    monkeypatch.setattr(prom_metrics, "sum_by", prom)
    with TestClient(app) as client:
        response = client.get("/api/fleet/graph")

    assert response.status_code == 200
    assert response.json()["stale"] is True
    assert prom_calls == 0


def test_prom_failure_serves_last_good_graph_without_writing_short_cache(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prometheus failure prefers the complete fallback over a node-only graph."""
    _seed_agent(db_conn)
    redis = _FakeRedis()
    last_good = _last_good_graph()
    prom_timeouts: list[float | None] = []

    import gateway.routers.fleet_graph as fg

    key = fg._cache_key(include_terminated=False, hours=None, decay_lambda=0.5)
    redis.values[f"fleet_graph:last_good:{key}"] = last_good.model_dump_json()
    patch_sync_redis(monkeypatch, _RedisFactory(redis))

    def boom(
        _metric: str,
        _by: str,
        *,
        window: timedelta | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, float]:
        prom_timeouts.append(timeout_s)
        raise httpx.ConnectError("prometheus unreachable")

    monkeypatch.setattr(prom_metrics, "sum_by", boom)
    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")

    expected = last_good.model_dump(mode="json")
    expected["stale"] = True
    assert resp.status_code == 200
    assert resp.json() == expected
    assert prom_timeouts == [8.0] * 4
    assert redis.writes == []


def test_prom_failure_without_last_good_keeps_pg_nodes_out_of_short_cache(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without last-good data, a Prometheus failure still preserves PG nodes."""
    agent_id = _seed_agent(db_conn)
    redis = _FakeRedis()

    patch_sync_redis(monkeypatch, _RedisFactory(redis))

    def boom(
        _metric: str,
        _by: str,
        *,
        window: timedelta | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, float]:
        raise httpx.ConnectError("prometheus unreachable")

    monkeypatch.setattr(prom_metrics, "sum_by", boom)
    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")

    assert resp.status_code == 200
    body = resp.json()
    assert {node["agent_id"] for node in body["nodes"]} == {agent_id}
    assert body["edges"] == []
    assert body["stale"] is True
    assert redis.writes == []


def test_pg_cancellation_serves_last_good_graph(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PG cancellation uses the complete fallback when no fresh nodes exist."""
    redis = _FakeRedis()
    last_good = _last_good_graph()

    import gateway.routers.fleet_graph as fg

    key = fg._cache_key(include_terminated=False, hours=None, decay_lambda=0.5)
    redis.values[f"fleet_graph:last_good:{key}"] = last_good.model_dump_json()
    patch_sync_redis(monkeypatch, _RedisFactory(redis))

    class _CanceledPool:
        def connection(self) -> object:
            raise pg_errors.QueryCanceled("canceling statement due to statement timeout")

        def close(self) -> None:
            pass

    with TestClient(app) as client:
        monkeypatch.setattr(app.state, "db_pool", _CanceledPool())
        resp = client.get("/api/fleet/graph")

    expected = last_good.model_dump(mode="json")
    expected["stale"] = True
    assert resp.status_code == 200
    assert resp.json() == expected
    assert redis.writes == []
