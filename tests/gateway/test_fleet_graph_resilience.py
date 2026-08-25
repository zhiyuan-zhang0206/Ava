"""Regression tests for fleet-graph upstream failures and Redis fallbacks."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors as pg_errors

from gateway import events_archive, loki_events, prom_metrics, telemetry_staleness
from gateway.app import app
from gateway.schemas import FleetGraphNode, FleetGraphResponse
from shared import telemetry
from shared.agents import AgentStatus
from shared.loki_index_labels import INDEX_LABEL_CUTOVER_AT


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


def _response_cache_writes(redis: _FakeRedis) -> list[tuple[str, str, int | None]]:
    """Exclude the independent frozen-source caches from response-cache assertions."""
    return [write for write in redis.writes if not write[0].startswith("fleet_graph:frozen:")]


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


def test_fleet_archive_boundary_reuses_the_process_cache() -> None:
    """Fleet's local helper cannot re-run the frozen archive max(ts) scan."""

    class Cursor:
        def __init__(self) -> None:
            self.executions = 0

        def execute(self, query: str) -> None:
            assert query == "SELECT max(ts) FROM events"
            self.executions += 1

        def fetchone(self) -> tuple[datetime]:
            return (datetime(2026, 8, 13, tzinfo=UTC),)

    import gateway.routers.fleet_graph as fg

    events_archive.reset_for_tests()
    first = Cursor()
    second = Cursor()
    assert fg._archive_boundary(first) == datetime(2026, 8, 13, tzinfo=UTC)
    assert fg._archive_boundary(second) == datetime(2026, 8, 13, tzinfo=UTC)
    assert first.executions == 1
    assert second.executions == 0


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


def _seed_archive_edge(
    db_conn: psycopg.Connection, *, source_agent: int, target_agent: int
) -> tuple[datetime, datetime]:
    edge_ts = datetime(2026, 8, 12, 10, tzinfo=UTC)
    boundary = datetime(2026, 8, 13, 10, tzinfo=UTC)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events "
            "(ts, agent_id, event_name, source, target_agent_id, machine, process, category, level) "
            "VALUES (%s, %s, 'spawn', 'test', %s, 'test', 'test', 'audit', 'info')",
            (edge_ts, source_agent, target_agent),
        )
        cur.execute(
            "INSERT INTO events "
            "(ts, agent_id, event_name, source, machine, process, category, level) "
            "VALUES (%s, NULL, 'turn_end', 'test', 'test', 'test', 'telemetry', 'info')",
            (boundary,),
        )
    db_conn.commit()
    return edge_ts, boundary


def test_pg_frozen_archive_cache_miss_populates_and_hit_skips_queries(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _seed_agent(db_conn)
    target = _seed_agent(db_conn)
    edge_ts, boundary = _seed_archive_edge(db_conn, source_agent=source, target_agent=target)
    redis = _FakeRedis()

    import gateway.routers.fleet_graph as fg

    monkeypatch.setattr(fg, "sync_redis", _RedisFactory(redis))
    with TestClient(app):
        first = fg._fetch_pg_graph(app.state.db_pool, not_terminated="")

        payload = json.loads(redis.values["fleet_graph:frozen:archive:v1"])
        assert payload == {
            "boundary": boundary.isoformat(),
            "rows": [[target, source, "spawn", edge_ts.isoformat()]],
        }
        assert redis.writes == [
            (
                "fleet_graph:frozen:archive:v1",
                redis.values["fleet_graph:frozen:archive:v1"],
                86_400,
            )
        ]

        def unexpected_source_read(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("frozen archive source queried on cache hit")

        monkeypatch.setattr(fg, "_archive_boundary", unexpected_source_read)
        monkeypatch.setattr(fg, "_fetch_archive_edges", unexpected_source_read)
        second = fg._fetch_pg_graph(app.state.db_pool, not_terminated="")

    expected_row = {
        "agent_id": source,
        "target_agent_id": target,
        "event_name": "spawn",
        "ts": edge_ts,
    }
    assert first.boundary == boundary
    assert first.archive_rows == [expected_row]
    assert second.boundary == boundary
    assert second.archive_rows == [expected_row]


def test_loki_legacy_cache_miss_populates_and_hit_skips_legacy_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FakeRedis()
    boundary = INDEX_LABEL_CUTOVER_AT - timedelta(days=2)
    now = INDEX_LABEL_CUTOVER_AT + timedelta(days=1)
    legacy_ts = boundary + timedelta(hours=1)
    indexed_ts = INDEX_LABEL_CUTOVER_AT + timedelta(hours=1)
    calls: list[dict[str, object]] = []

    import gateway.routers.fleet_graph as fg

    def query_events(**kwargs: object) -> tuple[list[dict[str, object]], bool]:
        calls.append(kwargs)
        if kwargs["from_"] == boundary:
            return [
                {
                    "agent_id": 1,
                    "target_agent_id": 2,
                    "event_name": "spawn",
                    "ts": legacy_ts,
                }
            ], False
        assert kwargs["from_"] == INDEX_LABEL_CUTOVER_AT
        return [
            {
                "agent_id": 3,
                "target_agent_id": 4,
                "event_name": "send_message",
                "ts": indexed_ts,
            }
        ], False

    monkeypatch.setattr(fg, "sync_redis", _RedisFactory(redis))
    monkeypatch.setattr(loki_events, "query_events", query_events)

    first_rows, first_truncated = fg._fetch_loki_edges(boundary=boundary, now=now)
    assert len(calls) == 2
    assert calls[0]["from_"] == boundary
    assert calls[0]["to"] == INDEX_LABEL_CUTOVER_AT
    assert calls[1]["from_"] == INDEX_LABEL_CUTOVER_AT
    assert calls[1]["to"] == now
    for call in calls:
        assert call["event_names"] == ["send_message", "spawn", "fork", "resurrect"]
        assert call["categories"] == ["audit"]
        assert call["limit"] == 50_000
        assert call["direction"] == "forward"
        assert call["timeout_s"] == 8.0
    assert json.loads(redis.values["fleet_graph:frozen:legacy:v1"]) == [
        [1, 2, "spawn", legacy_ts.isoformat()]
    ]
    assert redis.writes == [
        (
            "fleet_graph:frozen:legacy:v1",
            redis.values["fleet_graph:frozen:legacy:v1"],
            86_400,
        )
    ]

    calls.clear()
    second_rows, second_truncated = fg._fetch_loki_edges(boundary=boundary, now=now)

    assert len(calls) == 1
    assert calls[0]["from_"] == INDEX_LABEL_CUTOVER_AT
    assert first_rows == second_rows
    assert first_truncated is False
    assert second_truncated is False


def test_loki_split_counts_an_exact_cutover_row_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FakeRedis()
    boundary = INDEX_LABEL_CUTOVER_AT - timedelta(days=2)
    now = INDEX_LABEL_CUTOVER_AT + timedelta(days=1)
    cutover_row: dict[str, object] = {
        "agent_id": 1,
        "target_agent_id": 2,
        "event_name": "spawn",
        "ts": INDEX_LABEL_CUTOVER_AT,
    }

    import gateway.routers.fleet_graph as fg

    def query_events(**_kwargs: object) -> tuple[list[dict[str, object]], bool]:
        # Loki range endpoints are inclusive, so the same line can be returned
        # by both independently-issued slices at the exact cutover timestamp.
        return [cutover_row], False

    monkeypatch.setattr(fg, "sync_redis", _RedisFactory(redis))
    monkeypatch.setattr(loki_events, "query_events", query_events)

    rows, truncated = fg._fetch_loki_edges(boundary=boundary, now=now)

    assert rows == [cutover_row]
    assert truncated is False


def test_loki_legacy_cache_failure_queries_sources_and_returns_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = INDEX_LABEL_CUTOVER_AT - timedelta(days=2)
    now = INDEX_LABEL_CUTOVER_AT + timedelta(days=1)
    calls: list[tuple[datetime, datetime]] = []

    import gateway.routers.fleet_graph as fg

    def redis_down(*_args: object, **_kwargs: object) -> object:
        raise ConnectionError("redis down")

    def query_events(**kwargs: object) -> tuple[list[dict[str, object]], bool]:
        from_ = kwargs["from_"]
        to = kwargs["to"]
        assert isinstance(from_, datetime)
        assert isinstance(to, datetime)
        calls.append((from_, to))
        return [
            {
                "agent_id": len(calls),
                "target_agent_id": len(calls) + 10,
                "event_name": "spawn",
                "ts": from_ + timedelta(hours=1),
            }
        ], False

    monkeypatch.setattr(fg, "sync_redis", redis_down)
    monkeypatch.setattr(loki_events, "query_events", query_events)

    rows, truncated = fg._fetch_loki_edges(boundary=boundary, now=now)

    assert calls == [
        (boundary, INDEX_LABEL_CUTOVER_AT),
        (INDEX_LABEL_CUTOVER_AT, now),
    ]
    assert [row["agent_id"] for row in rows] == [1, 2]
    assert truncated is False


@pytest.mark.parametrize("truncated_slice", ["legacy", "indexed"])
def test_loki_split_reports_truncation_from_either_slice(
    truncated_slice: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    redis = _FakeRedis()
    boundary = INDEX_LABEL_CUTOVER_AT - timedelta(days=2)
    now = INDEX_LABEL_CUTOVER_AT + timedelta(days=1)

    import gateway.routers.fleet_graph as fg

    def query_events(**kwargs: object) -> tuple[list[dict[str, object]], bool]:
        era = "legacy" if kwargs["from_"] == boundary else "indexed"
        return [], era == truncated_slice

    monkeypatch.setattr(fg, "sync_redis", _RedisFactory(redis))
    monkeypatch.setattr(loki_events, "query_events", query_events)

    _rows, truncated = fg._fetch_loki_edges(boundary=boundary, now=now)

    assert truncated is True


def _empty_prom(
    _metric: str,
    _by: str,
    *,
    window: timedelta | None = None,
    timeout_s: float | None = None,
) -> dict[str, float]:
    return {}


def _empty_loki(**_kwargs: object) -> tuple[list[dict[str, object]], bool]:
    return [], False


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

    monkeypatch.setattr(fg, "sync_redis", _RedisFactory(redis))
    monkeypatch.setattr(loki_events, "query_events", _empty_loki)
    monkeypatch.setattr(prom_metrics, "sum_by", _empty_prom)
    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")

    assert resp.status_code == 200
    key = fg._cache_key(include_terminated=False, hours=None, decay_lambda=0.5)
    response_writes = _response_cache_writes(redis)
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

    monkeypatch.setattr(fg, "sync_redis", _RedisFactory(redis))
    monkeypatch.setattr(loki_events, "query_events", _empty_loki)
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
    assert {write[0] for write in _response_cache_writes(redis)} == {
        key,
        f"fleet_graph:last_good:{key}",
    }
    assert redis.values[key] == redis.values[f"fleet_graph:last_good:{key}"]
    assert emitted[0][0] == "telemetry_read_stale"
    assert emitted[0][1]["source"] == "prometheus"


def test_loki_failure_serves_last_good_graph_without_writing_short_cache(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cached full graph survives an unavailable live Loki tail."""
    _seed_agent(db_conn)
    redis = _FakeRedis()
    query_args: dict[str, object] = {}
    snapshot_at = datetime(2026, 8, 24, 23, 31, tzinfo=UTC)
    last_good = _last_good_graph().model_copy(
        update={"truncated": True, "snapshot_at": snapshot_at}
    )

    import gateway.routers.fleet_graph as fg

    key = fg._cache_key(include_terminated=False, hours=None, decay_lambda=0.5)
    serialized_last_good = last_good.model_dump(mode="json")
    redis.values[f"fleet_graph:last_good:{key}"] = json.dumps(serialized_last_good)
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
    expected["truncated"] = False
    assert resp.status_code == 200
    assert resp.json() == expected
    assert query_args["timeout_s"] == 8.0
    assert _response_cache_writes(redis) == []


def test_loki_edge_cap_marks_a_fresh_graph_truncated(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edge fetch's lookahead is visible as incomplete graph data."""
    _seed_agent(db_conn)
    redis = _FakeRedis()

    import gateway.routers.fleet_graph as fg

    def truncated_query_events(**_kwargs: object) -> tuple[list[dict[str, object]], bool]:
        return [], True

    monkeypatch.setattr(fg, "sync_redis", _RedisFactory(redis))
    monkeypatch.setattr(prom_metrics, "sum_by", _empty_prom)
    monkeypatch.setattr(loki_events, "query_events", truncated_query_events)
    with TestClient(app) as client:
        response = client.get("/api/fleet/graph")

    assert response.status_code == 200
    assert response.json()["stale"] is True
    assert response.json()["truncated"] is True
    key = fg._cache_key(include_terminated=False, hours=None, decay_lambda=0.5)
    assert {write[0] for write in _response_cache_writes(redis)} == {key}
    assert redis.values[key]


def test_pg_phase_exceeding_route_budget_serves_stale_before_telemetry(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow DB phase does not start more upstream reads after its deadline."""
    _seed_agent(db_conn)
    redis = _FakeRedis()
    prom_calls = 0

    import gateway.routers.fleet_graph as fg

    monotonic = iter((0.0, fg._ROUTE_TIMEOUT_S + 0.1))
    monkeypatch.setattr(fg, "sync_redis", _RedisFactory(redis))
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
    assert response.json()["truncated"] is False
    assert prom_calls == 0


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
    assert _response_cache_writes(redis) == []


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
    monkeypatch.setattr(fg, "sync_redis", _RedisFactory(redis))

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
    assert _response_cache_writes(redis) == []


def test_prom_failure_without_last_good_keeps_pg_nodes_out_of_short_cache(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without last-good data, a Prometheus failure still preserves PG nodes."""
    agent_id = _seed_agent(db_conn)
    redis = _FakeRedis()

    import gateway.routers.fleet_graph as fg

    monkeypatch.setattr(fg, "sync_redis", _RedisFactory(redis))

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
    assert _response_cache_writes(redis) == []


def test_pg_cancellation_serves_last_good_graph(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PG cancellation uses the complete fallback when no fresh nodes exist."""
    redis = _FakeRedis()
    last_good = _last_good_graph()

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
