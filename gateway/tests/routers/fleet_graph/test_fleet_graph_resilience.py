"""Regression tests for fleet-graph read failures and the Redis poll cache."""

from datetime import datetime

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors as pg_errors

from base.api_contracts.agent_contract import AgentStatus, LivenessState
from base.events.live.tests.fakes import patch_sync_redis
from base.telemetry.tests.capture.event_capture import capturing_events
from gateway.app import app
from gateway.schemas.fleet_graph import FleetGraphNode, FleetGraphResponse
from gateway.tests.routers.fleet_graph.staleness_support import use_heartbeat_age


def _fresh_heartbeat_age(pool: object, *, now: datetime) -> float:
    del pool, now
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
    """Existing resilience cases isolate their named failure."""
    use_heartbeat_age(monkeypatch, _fresh_heartbeat_age)


def _seed_agent(db_conn: psycopg.Connection) -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        agent_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, born_spawner, status) VALUES (%s, 'test', 'test', 'running')",
            (agent_id,),
        )
    db_conn.commit()
    return agent_id


def test_success_writes_only_the_short_poll_cache(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full graph refresh writes the 60-second poll cache and nothing longer-lived."""
    _seed_agent(db_conn)
    redis = _FakeRedis()

    import gateway.routers.fleet_graph as fg

    patch_sync_redis(monkeypatch, _RedisFactory(redis))
    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")

    assert resp.status_code == 200
    key = fg.cache_key(include_terminated=False, hours=None, decay_lambda=0.5)
    assert [(write[0], write[2]) for write in redis.writes] == [(key, 60)]


def test_stale_heartbeat_marks_telemetry_and_keeps_fresh_graph_cached(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heartbeat outage marks telemetry degradation without discarding the graph."""
    _seed_agent(db_conn)
    redis = _FakeRedis()

    import gateway.routers.fleet_graph as fg

    patch_sync_redis(monkeypatch, _RedisFactory(redis))

    def missing_heartbeat(pool: object, *, now: datetime) -> None:
        del pool, now

    use_heartbeat_age(monkeypatch, missing_heartbeat)

    # The capture is bound inside the app's lifespan: the gateway binds its own pipeline on start.
    with TestClient(app) as client, capturing_events() as emitted_events:
        resp = client.get("/api/fleet/graph")
        stale = emitted_events.attributes("telemetry_read_stale")

    assert resp.status_code == 200
    body = resp.json()
    assert body["telemetry_stale"] is True
    assert body["snapshot_at"] is not None
    key = fg.cache_key(include_terminated=False, hours=None, decay_lambda=0.5)
    assert {write[0] for write in redis.writes} == {key}
    assert stale[0]["source"] == "postgres"


def test_pg_cancellation_is_a_500_and_ignores_leftover_last_good_entries(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canceled PG read fails the request even when Redis still holds an old
    long-lived entry for the same parameters, and writes nothing."""
    redis = _FakeRedis()
    leftover = FleetGraphResponse(
        nodes=[
            FleetGraphNode(
                agent_id=999,
                label="leftover",
                status=AgentStatus.RUNNING,
                liveness_state=LivenessState.ONLINE,
                spawner="test",
                machine=None,
                node_score=12.5,
                total_tokens=42,
            )
        ],
        edges=[],
    )

    import gateway.routers.fleet_graph as fg

    key = fg.cache_key(include_terminated=False, hours=None, decay_lambda=0.5)
    redis.values[f"fleet_graph:last_good:{key}"] = leftover.model_dump_json()
    patch_sync_redis(monkeypatch, _RedisFactory(redis))

    class _CanceledPool:
        def connection(self) -> object:
            raise pg_errors.QueryCanceled("canceling statement due to statement timeout")

        def close(self) -> None:
            pass

    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(app.state, "db_pool", _CanceledPool())
        resp = client.get("/api/fleet/graph")

    assert resp.status_code == 500
    assert "leftover" not in resp.text
    assert redis.writes == []
