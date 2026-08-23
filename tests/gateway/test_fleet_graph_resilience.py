"""Regression tests for fleet-graph upstream failures and Redis fallbacks."""

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors as pg_errors

from gateway import loki_events, prom_metrics
from gateway.app import app
from gateway.schemas import FleetGraphNode, FleetGraphResponse
from shared.agents import AgentStatus


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


def _empty_prom(_metric: str, _by: str, *, window_hours: int | None = None) -> dict[str, float]:
    return {}


def _empty_loki(**_kwargs: object) -> tuple[list[dict[str, object]], bool]:
    return [], False


def test_success_writes_short_cache_and_last_good_graph(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full graph refresh updates both the poll cache and its last-good fallback."""
    _seed_agent(db_conn)
    redis = _FakeRedis()

    import gateway.routers.fleet_graph as fg

    monkeypatch.setattr(fg, "sync_redis", _RedisFactory(redis))
    monkeypatch.setattr(loki_events, "query_events", _empty_loki)
    monkeypatch.setattr(prom_metrics, "sum_by", _empty_prom)
    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")

    assert resp.status_code == 200
    key = fg._cache_key(include_terminated=False, hours=None, decay_lambda=0.5)
    assert {write[0] for write in redis.writes} == {key, f"fleet_graph:last_good:{key}"}
    assert redis.values[key] == redis.values[f"fleet_graph:last_good:{key}"]
    assert {write[0]: write[2] for write in redis.writes} == {
        key: fg._CACHE_TTL_SECONDS,
        f"fleet_graph:last_good:{key}": fg._LAST_GOOD_CACHE_TTL_SECONDS,
    }


def test_loki_failure_serves_last_good_graph_without_writing_short_cache(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cached full graph survives an unavailable live Loki tail."""
    _seed_agent(db_conn)
    redis = _FakeRedis()
    query_args: dict[str, object] = {}
    last_good = FleetGraphResponse(
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

    import gateway.routers.fleet_graph as fg

    key = fg._cache_key(include_terminated=False, hours=None, decay_lambda=0.5)
    redis.values[f"fleet_graph:last_good:{key}"] = last_good.model_dump_json()
    monkeypatch.setattr(fg, "sync_redis", _RedisFactory(redis))
    monkeypatch.setattr(prom_metrics, "sum_by", _empty_prom)

    def boom(**kwargs: object) -> object:
        query_args.update(kwargs)
        raise httpx.ConnectError("loki unreachable")

    monkeypatch.setattr(loki_events, "query_events", boom)
    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")

    expected = last_good.model_dump(mode="json")
    expected["stale"] = True
    assert resp.status_code == 200
    assert resp.json() == expected
    assert query_args["timeout_s"] == 8.0
    assert redis.writes == []


def test_loki_failure_without_last_good_keeps_fetched_nodes_out_of_short_cache(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a full fallback, a failed tail still returns the PG/Prom node set."""
    agent_id = _seed_agent(db_conn)
    redis = _FakeRedis()

    import gateway.routers.fleet_graph as fg

    monkeypatch.setattr(fg, "sync_redis", _RedisFactory(redis))
    monkeypatch.setattr(prom_metrics, "sum_by", _empty_prom)

    def boom(**_kwargs: object) -> object:
        raise httpx.ConnectError("loki unreachable")

    monkeypatch.setattr(loki_events, "query_events", boom)
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
    last_good = FleetGraphResponse(
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

    import gateway.routers.fleet_graph as fg

    key = fg._cache_key(include_terminated=False, hours=None, decay_lambda=0.5)
    redis.values[f"fleet_graph:last_good:{key}"] = last_good.model_dump_json()
    monkeypatch.setattr(fg, "sync_redis", _RedisFactory(redis))

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
