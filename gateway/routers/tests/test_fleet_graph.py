"""GET /api/fleet/graph integration tests.

FastAPI TestClient + real ava_test DB. The SQL is the one place a column-name /
cast / filter typo passes the frontend tests (which feed mock data) but breaks
live, so it is exercised against a real DB here. Covers:
- `total_tokens` — per-agent retained-window (7d) in+out llm_usage sums, read from
  `telemetry_events`.
- `node_score` — windowed SUM(in)*0.1 + SUM(out)*1.0 (drives node size).
- edge weight — lineage (spawn/fork/resurrect) permanent count*2.0 (no decay,
  always shown); message (send_message) recency-decayed, dropped below 0.01.
  Edges are aggregated in Postgres from `audit_events` (the audit record).
- a NULL agent_id audit row never 500s the endpoint.
"""

import json
import math
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors as pg_errors

from gateway.app import app
from gateway.events import audit_rows
from gateway.lgtm import telemetry_staleness


def _seed_agent(
    db_conn: psycopg.Connection,
    *,
    status: str = "running",
    spawner: str = "test",
    born_spawner: str | None = None,
) -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, born_spawner, status) VALUES (%s, %s, %s, %s)",
            (new_id, spawner, born_spawner, status),
        )
    db_conn.commit()
    return new_id


def _fresh_heartbeat_age(pool: object, *, now: datetime) -> float:
    del pool, now
    return 30.0


@pytest.fixture(autouse=True)
def _fresh_telemetry_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing route tests describe fresh-source behavior."""
    monkeypatch.setattr(telemetry_staleness, "heartbeat_age", _fresh_heartbeat_age)
    monkeypatch.setattr(telemetry_staleness, "_source_states", {})
    monkeypatch.setattr(telemetry_staleness, "CHECK_INTERVAL_S", 0, raising=False)


def _usage(
    db_conn: psycopg.Connection,
    agent: int,
    *,
    in_total: int = 0,
    out_total: int = 0,
    age_hours: float = 0.0,
    event: str = "llm_usage",
) -> None:
    """Record one `llm_usage` telemetry row (or another event carrying token fields)."""
    db_conn.execute(
        "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
        "category, event_name, level, source, attributes) VALUES (%s, now() - (%s * interval "
        "'1 hour'), %s, 'test', 'c', 'test', 'telemetry', %s, 'info', 'test', %s::jsonb)",
        (
            uuid.uuid4().int % (1 << 62),
            age_hours,
            agent,
            event,
            json.dumps({"in_total": in_total, "out_total": out_total}),
        ),
    )
    db_conn.commit()


def _event(
    db_conn: psycopg.Connection,
    *,
    source_agent: int | None,
    target_agent: int | None,
    event_type: str,
    age_hours: float = 0.0,
) -> None:
    """Record one audit event (a directed inter-agent operation) `age_hours` ago."""
    db_conn.execute(
        "INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, source, "
        "agent_id, target_agent_id) "
        "VALUES (%s, now() - (%s * interval '1 hour'), 'test', 'test', %s, 'info', 'test', %s, %s)",
        (uuid.uuid4().int % (1 << 62), age_hours, event_type, source_agent, target_agent),
    )
    db_conn.commit()


def test_decay_lambda_comes_from_display_config(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitted `?decay_lambda=` is settings.display.fleet_graph_decay_lambda
    (``AVA_FLEET_GRAPH_DECAY_LAMBDA``); the literal 0.5 is only that field's
    default, not a hard-coded decay constant. A zero decay keeps an old message
    edge above the 0.01 drop threshold."""
    from base.config import settings

    source = _seed_agent(db_conn)
    target = _seed_agent(db_conn)
    _event(
        db_conn,
        source_agent=source,
        target_agent=target,
        event_type="send_message",
        age_hours=360,
    )

    monkeypatch.setattr(settings.display, "fleet_graph_decay_lambda", 0.0)
    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")

    assert resp.status_code == 200, resp.text
    edges = resp.json()["edges"]
    assert len(edges) == 1  # exp(0 * 15d) = 1.0 — above the 0.01 drop threshold


def _nodes_by_id(client: TestClient, query: str = "") -> dict[int, dict]:
    resp = client.get(f"/api/fleet/graph{query}")
    assert resp.status_code == 200, resp.text
    return {n["agent_id"]: n for n in resp.json()["nodes"]}


def test_nodes_use_immutable_birth_parent(db_conn: psycopg.Connection) -> None:
    folded_parent = _seed_agent(db_conn)
    birth_parent = _seed_agent(db_conn)
    child = _seed_agent(
        db_conn,
        spawner=f"agent:{folded_parent}",
        born_spawner=f"agent:{birth_parent}",
    )

    with TestClient(app) as client:
        nodes = _nodes_by_id(client)

    assert nodes[child]["spawner"] == f"agent:{birth_parent}"


def test_total_tokens_sums_in_plus_out(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    _usage(db_conn, a, in_total=300, out_total=80)

    with TestClient(app) as client:
        nodes = _nodes_by_id(client)

    assert nodes[a]["total_tokens"] == 380  # in 300 + out 80


def test_total_tokens_reads_the_retained_seven_day_window(db_conn: psycopg.Connection) -> None:
    """Rows inside 7 days count toward `total_tokens`; older rows do not, though the
    all-time node score still carries them."""
    a = _seed_agent(db_conn)
    _usage(db_conn, a, in_total=100, out_total=10, age_hours=24 * 6)
    _usage(db_conn, a, in_total=1000, out_total=1000, age_hours=24 * 8)

    with TestClient(app) as client:
        nodes = _nodes_by_id(client)

    assert nodes[a]["total_tokens"] == 110
    assert nodes[a]["node_score"] == 100 * 0.1 + 10 + 1000 * 0.1 + 1000


def test_node_exposes_canonical_status_and_independent_liveness(
    db_conn: psycopg.Connection,
) -> None:
    a = _seed_agent(db_conn, status="idling")
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET liveness_state = 'offline' WHERE id = %s",
            (a,),
        )
    db_conn.commit()

    with TestClient(app) as client:
        nodes = _nodes_by_id(client)

    assert nodes[a]["status"] == "idling"
    assert nodes[a]["liveness_state"] == "offline"


def test_total_tokens_zero_without_usage(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)

    with TestClient(app) as client:
        nodes = _nodes_by_id(client)
    assert nodes[a]["total_tokens"] == 0


def test_total_tokens_comes_from_llm_usage_rows_only(db_conn: psycopg.Connection) -> None:
    """Only `llm_usage` rows feed the token totals: another event carrying the same
    fields never leaks in."""
    a = _seed_agent(db_conn)
    _usage(db_conn, a, in_total=100, out_total=50)
    _usage(db_conn, a, in_total=7777, out_total=7777, event="turn_end")

    with TestClient(app) as client:
        nodes = _nodes_by_id(client)

    assert nodes[a]["total_tokens"] == 150


def test_total_tokens_scoped_per_agent(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _usage(db_conn, a, in_total=100, out_total=50)
    _usage(db_conn, b, in_total=1, out_total=1)

    with TestClient(app) as client:
        nodes = _nodes_by_id(client)

    assert nodes[a]["total_tokens"] == 150
    assert nodes[b]["total_tokens"] == 2


# ── node_score: windowed weighted token work ──────────────────────────────


def test_node_score_weights_output_ten_times_input(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    _usage(db_conn, a, in_total=300, out_total=80)

    with TestClient(app) as client:
        nodes = _nodes_by_id(client)

    # SUM(in)*0.1 + SUM(out)*1.0 = 300*0.1 + 80*1.0 = 30 + 80 = 110
    assert nodes[a]["node_score"] == 110.0


def test_node_score_zero_without_usage(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)

    with TestClient(app) as client:
        nodes = _nodes_by_id(client)
    assert nodes[a]["node_score"] == 0.0


def test_node_score_windowed_excludes_old_events(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    # The retained 7d total carries the old + recent rows; the 24h score only the recent one.
    _usage(db_conn, a, in_total=100, out_total=100, age_hours=1)
    _usage(db_conn, a, in_total=999, out_total=999, age_hours=48)

    with TestClient(app) as client:
        nodes = _nodes_by_id(client, "?hours=24")

    # Only the recent event scores: 100*0.1 + 100*1.0 = 110. total_tokens keeps
    # both events because they fall inside its retained 7d window.
    assert nodes[a]["node_score"] == 110.0
    assert nodes[a]["total_tokens"] == 100 + 100 + 999 + 999


def test_node_drops_degree_fields(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    with TestClient(app) as client:
        nodes = _nodes_by_id(client)
    assert "degree_in" not in nodes[a]
    assert "degree_out" not in nodes[a]
    assert "node_score" in nodes[a]


# ── edge weight: per-event sum-of-exponentials decay ──────────────────────


def _edges_by_type(client: TestClient, query: str = "") -> dict[str, dict]:
    resp = client.get(f"/api/fleet/graph{query}")
    assert resp.status_code == 200, resp.text
    return {e["event_type"]: e for e in resp.json()["edges"]}


def test_edge_weight_type_multiplier_fresh(db_conn: psycopg.Connection) -> None:
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    _event(db_conn, source_agent=s, target_agent=c, event_type="spawn")
    _event(db_conn, source_agent=s, target_agent=c, event_type="send_message")

    with TestClient(app) as client:
        edges = _edges_by_type(client)

    # Fresh single events: lineage weight = COUNT(*) * 2.0 = 2.0 (permanent);
    # message weight = EXP(0) * 1.0 = 1.0 (decayed, but age 0 -> factor 1).
    assert edges["spawn"]["weight"] == 2.0
    assert edges["send_message"]["weight"] == 1.0
    assert edges["spawn"]["event_count"] == 1


def test_lineage_edge_permanent_no_decay_always_shown(db_conn: psycopg.Connection) -> None:
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # A spawn from ~83 days ago (archive era). Under recency-decay weighting this
    # would decay far below the 0.01 threshold and vanish; lineage is permanent —
    # weight = COUNT(*) * 2.0, no time decay, and never filtered by the HAVING.
    _event(db_conn, source_agent=s, target_agent=c, event_type="spawn", age_hours=2000)

    with TestClient(app) as client:
        edges = _edges_by_type(client)

    assert edges["spawn"]["weight"] == 2.0
    assert edges["spawn"]["event_count"] == 1


def test_resurrect_edge_included_as_permanent_lineage(db_conn: psycopg.Connection) -> None:
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # resurrect is a lineage tie now included in the graph (it was missing before).
    _event(db_conn, source_agent=s, target_agent=c, event_type="resurrect")
    _event(db_conn, source_agent=s, target_agent=c, event_type="resurrect")

    with TestClient(app) as client:
        edges = _edges_by_type(client)

    # Permanent weight = COUNT(*) * 2.0 = 2 * 2.0 = 4.0.
    assert edges["resurrect"]["weight"] == 4.0
    assert edges["resurrect"]["event_count"] == 2


def test_lineage_edge_not_excluded_by_time_window(db_conn: psycopg.Connection) -> None:
    """Lineage (spawn/fork/resurrect) edges survive the time-window filter.

    The `?hours=` window only gates send_message events; lineage edges
    are permanent and always returned regardless of age.
    """
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # A spawn from 100 hours ago — well beyond a 24h window. A send_message
    # at the same age is filtered, but the lineage edge must still appear.
    _event(db_conn, source_agent=s, target_agent=c, event_type="spawn", age_hours=100)

    with TestClient(app) as client:
        edges = _edges_by_type(client, "?hours=24")

    # Lineage edge survives the time window.
    assert "spawn" in edges
    assert edges["spawn"]["weight"] == 2.0
    assert edges["spawn"]["event_count"] == 1


def test_message_edge_below_threshold_filtered(db_conn: psycopg.Connection) -> None:
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # A single ~83-day-old message decays below 0.01 -> dropped. (A lineage edge at
    # the same age still shows; only messages are thresholded.)
    _event(db_conn, source_agent=s, target_agent=c, event_type="send_message", age_hours=2000)

    with TestClient(app) as client:
        edges = _edges_by_type(client)

    assert "send_message" not in edges


def test_edge_weight_sums_per_event_with_decay(db_conn: psycopg.Connection) -> None:
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # A fresh message and one 48h old: the weight sums both per-row decays.
    _event(db_conn, source_agent=s, target_agent=c, event_type="send_message")
    _event(db_conn, source_agent=s, target_agent=c, event_type="send_message", age_hours=48)

    with TestClient(app) as client:
        edges = _edges_by_type(client)

    expected = 1.0 + math.exp(-0.5 * 2.0)
    assert edges["send_message"]["weight"] == pytest.approx(expected, abs=1e-3)  # pyright: ignore[reportUnknownMemberType]
    assert edges["send_message"]["event_count"] == 2


def test_edge_aggregation_applies_window_live_filter_and_weights(
    db_conn: psycopg.Connection,
) -> None:
    """The SQL aggregate is the one place the weights are computed: lineage is
    all-time and 2.0 each, messages decay and respect the window, and the live
    endpoint filter drops an edge whose other end is not live."""
    now = datetime.now(UTC)
    _event(db_conn, source_agent=1, target_agent=2, event_type="spawn", age_hours=24 * 30)
    _event(db_conn, source_agent=1, target_agent=2, event_type="send_message", age_hours=48)
    _event(db_conn, source_agent=1, target_agent=2, event_type="send_message", age_hours=96)
    _event(db_conn, source_agent=1, target_agent=9, event_type="fork")

    rows = audit_rows.edge_weights(
        db_conn,
        live_ids={1, 2},
        win_start=now - timedelta(days=3),
        now=now,
        decay_lambda=0.5,
    )

    by_type = {name: (weight, count) for _target, _agent, name, weight, count, _last in rows}
    assert by_type["spawn"] == (2.0, 1)
    message_weight, message_count = by_type["send_message"]
    assert message_weight == pytest.approx(math.exp(-1.0), abs=1e-3)  # pyright: ignore[reportUnknownMemberType]
    assert message_count == 1  # the 96h-old message is outside the 3-day window
    assert "fork" not in by_type


def test_edge_window_excludes_old_events(db_conn: psycopg.Connection) -> None:
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # A 100h-old LIVE message (the live stream carries any post-freeze age).
    _event(db_conn, source_agent=s, target_agent=c, event_type="send_message", age_hours=100)

    with TestClient(app) as client:
        # 24h window excludes the 100h-old event -> no edges.
        assert _edges_by_type(client, "?hours=24") == {}
        # All-time still sees it.
        assert "send_message" in _edges_by_type(client)


def test_decay_lambda_param_steepens_decay(db_conn: psycopg.Connection) -> None:
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    _event(db_conn, source_agent=s, target_agent=c, event_type="send_message", age_hours=48)

    with TestClient(app) as client:
        gentle = _edges_by_type(client, "?decay_lambda=0.1")["send_message"]["weight"]
        steep = _edges_by_type(client, "?decay_lambda=2.0")["send_message"]["weight"]

    # A larger lambda decays an aged event harder -> smaller weight.
    assert steep < gentle


def test_decay_lambda_is_quantized_for_edge_computation(
    db_conn: psycopg.Connection,
) -> None:
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    _event(db_conn, source_agent=s, target_agent=c, event_type="send_message", age_hours=48)

    with TestClient(app) as client:
        weight = _edges_by_type(client, "?decay_lambda=0.551")["send_message"]["weight"]

    assert weight == pytest.approx(math.exp(-0.55 * 2.0), abs=1e-4)  # pyright: ignore[reportUnknownMemberType]


def test_decay_lambda_quantization_aliases_cache_key(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    _usage(db_conn, a, in_total=100)

    with TestClient(app) as client:
        first = client.get("/api/fleet/graph", params={"decay_lambda": 0.55})
        assert first.status_code == 200

        # A cache miss would expose this changed upstream value.
        _usage(db_conn, a, in_total=9999)
        second = client.get("/api/fleet/graph", params={"decay_lambda": 0.551})

    assert second.status_code == 200
    assert second.json() == first.json()


def test_decay_lambda_above_maximum_is_rejected() -> None:
    with TestClient(app) as client:
        response = client.get("/api/fleet/graph", params={"decay_lambda": 10.01})

    assert response.status_code == 422


# ── terminated endpoint filtering (merge layer) ────────────────────────


def test_edges_touching_terminated_agent_excluded_by_default(db_conn: psycopg.Connection) -> None:
    """The default graph excludes terminated agents; an edge that touches one
    can never be drawn (its endpoint is not in the node set). The shared merge
    loop drops it for both archive and Loki rows."""
    live = _seed_agent(db_conn)
    dead = _seed_agent(db_conn, status="terminated")
    _event(db_conn, source_agent=live, target_agent=dead, event_type="spawn")
    _event(db_conn, source_agent=dead, target_agent=live, event_type="send_message")

    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")
    assert resp.status_code == 200
    body = resp.json()
    assert {n["agent_id"] for n in body["nodes"]} == {live}
    assert body["edges"] == []


def test_live_node_with_terminated_spawner_shows_isolated(db_conn: psycopg.Connection) -> None:
    """Task #1089/#1104 regression — the #2753 shape: a live agent whose
    spawner has since terminated. The live node renders on its
    own; the terminated partner is NOT a node and the spawn edge is NOT
    returned (user ruling 2026-08-09: terminated agents never appear in the
    graph; a live node with no live parent simply shows without the edge)."""
    live = _seed_agent(db_conn, status="idling")
    dead = _seed_agent(db_conn, status="terminated")
    _event(db_conn, source_agent=dead, target_agent=live, event_type="spawn")

    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")
    assert resp.status_code == 200
    body = resp.json()
    assert {n["agent_id"] for n in body["nodes"]} == {live}
    assert body["edges"] == []


def test_edge_between_two_terminated_agents_excluded_by_default(
    db_conn: psycopg.Connection,
) -> None:
    d1 = _seed_agent(db_conn, status="terminated")
    d2 = _seed_agent(db_conn, status="terminated")
    _event(db_conn, source_agent=d1, target_agent=d2, event_type="spawn")

    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")
    assert resp.status_code == 200
    assert resp.json()["edges"] == []


def test_include_terminated_returns_terminated_endpoint_edges(db_conn: psycopg.Connection) -> None:
    """?include_terminated=true restores the full edge set (lineage archive
    mode) — the filter is the same switch that governs the node set."""
    live = _seed_agent(db_conn)
    dead = _seed_agent(db_conn, status="terminated")
    _event(db_conn, source_agent=live, target_agent=dead, event_type="spawn")

    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph?include_terminated=true")
    assert resp.status_code == 200
    body = resp.json()
    assert {n["agent_id"] for n in body["nodes"]} == {live, dead}
    assert len(body["edges"]) == 1
    assert body["edges"][0]["event_type"] == "spawn"


def test_live_live_edge_still_returned_after_filter(db_conn: psycopg.Connection) -> None:
    """The filter only drops terminated endpoints — a live-live edge must
    survive unchanged (weight semantics untouched)."""
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    _event(db_conn, source_agent=s, target_agent=c, event_type="spawn")
    _event(db_conn, source_agent=s, target_agent=c, event_type="send_message")

    with TestClient(app) as client:
        edges = _edges_by_type(client)

    assert edges["spawn"]["weight"] == 2.0
    assert edges["send_message"]["weight"] == 1.0


# -- an audit row with no agent never forms an edge ---


def test_null_agent_id_audit_row_does_not_500_and_makes_no_edge(
    db_conn: psycopg.Connection,
) -> None:
    """An audit row whose agent_id is NULL (service-level event — the W9
    telemetry change first allowed such rows to land) must not crash the
    graph endpoint: the edge query filters agent_id IS NOT NULL, so the
    NULL to_agent never reaches the pydantic int field."""
    t = _seed_agent(db_conn)  # target side is a real agent
    _event(db_conn, source_agent=None, target_agent=t, event_type="send_message")

    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")
    assert resp.status_code == 200
    assert resp.json()["edges"] == []


# ── Redis cache: 60s TTL, keyed by params, fail-open ──────────────────────


def test_cache_serves_stale_graph_within_ttl(db_conn: psycopg.Connection) -> None:
    """A second request within the 60s TTL hits the Redis cache and does not
    re-query: add usage after the first request and assert the response still
    carries the first request's data."""
    a = _seed_agent(db_conn)
    _usage(db_conn, a, in_total=100, out_total=50)

    with TestClient(app) as client:
        first = _nodes_by_id(client)

    # Usage added after the first request — must NOT be visible.
    _usage(db_conn, a, in_total=9999, out_total=9999)

    with TestClient(app) as client:
        second = _nodes_by_id(client)

    assert first[a]["total_tokens"] == 150
    assert second[a]["total_tokens"] == 150  # cached, not 19998


def test_cache_key_separates_params(db_conn: psycopg.Connection) -> None:
    """Different query params get different cache keys: a 24h-window request
    must not serve the all-time response (nor vice versa)."""
    a = _seed_agent(db_conn)
    _usage(db_conn, a, in_total=100, out_total=50, age_hours=1)
    _usage(db_conn, a, in_total=100, out_total=100, age_hours=48)

    with TestClient(app) as client:
        all_time = _nodes_by_id(client)
        windowed = _nodes_by_id(client, "?hours=24")

    # All-time includes the old increment; the 24h window excludes it.
    assert all_time[a]["total_tokens"] == 350
    assert all_time[a]["node_score"] == 170.0  # (200)*0.1 + (150)*1.0
    assert windowed[a]["node_score"] == 60.0  # only the fresh increment: 100*0.1 + 50*1.0


def test_cache_fail_open_when_redis_down(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Redis outage (sync_redis raising) degrades to a direct DB query —
    never a 500."""
    source = _seed_agent(db_conn)
    target = _seed_agent(db_conn)
    _event(db_conn, source_agent=source, target_agent=target, event_type="spawn", age_hours=48)
    _event(
        db_conn,
        source_agent=source,
        target_agent=target,
        event_type="send_message",
    )
    _usage(db_conn, source, in_total=100, out_total=50)

    import gateway.routers.fleet_graph as fg

    def boom(*args: object, **kwargs: object) -> object:
        raise ConnectionError("redis down")

    monkeypatch.setattr(fg, "sync_redis", boom)
    with TestClient(app) as client:
        response = client.get("/api/fleet/graph")

    assert response.status_code == 200
    body = response.json()
    nodes = {node["agent_id"]: node for node in body["nodes"]}
    edges = {edge["event_type"]: edge for edge in body["edges"]}
    assert nodes[source]["total_tokens"] == 150
    assert edges["spawn"]["weight"] == 2.0
    assert edges["send_message"]["weight"] == 1.0


def test_query_canceled_degrades_to_empty_graph(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A statement-timeout cancellation returns an empty graph (200), not a
    500 — and marks it `stale` so the frontend can tell "no data" from
    "query killed under load" (R4 layer 2, audit P2-10)."""

    class _CanceledPool:
        def connection(self) -> object:
            raise pg_errors.QueryCanceled("canceling statement due to statement timeout")

        def close(self) -> None:
            pass

    with TestClient(app) as client:
        # The lifespan startup assigns the real pool; override it inside the
        # client context so the teardown close() still runs against our stub.
        monkeypatch.setattr(app.state, "db_pool", _CanceledPool())
        resp = client.get("/api/fleet/graph")

    assert resp.status_code == 200
    assert resp.json() == {
        "nodes": [],
        "edges": [],
        "stale": True,
        "telemetry_stale": False,
        "snapshot_at": None,
    }


# ── audit gateway.md P2-10: failed != empty (R4 layer 2) ───────────────


def test_query_canceled_degrades_with_stale_flag(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A statement-timeout cancellation degrades to a VISIBLE empty graph
    (stale=True) — not an indistinguishable empty fleet. The frontend must
    be able to tell "the cluster has no data" from "the query was killed
    under load"."""

    class _BoomCursor:
        def __enter__(self) -> "_BoomCursor":
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def execute(self, *_a: object, **_k: object) -> None:
            raise pg_errors.QueryCanceled("canceling statement due to statement timeout")

    class _BoomConn:
        def __enter__(self) -> "_BoomConn":
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def cursor(self) -> _BoomCursor:
            return _BoomCursor()

    class _BoomPool:
        def connection(self) -> _BoomConn:
            return _BoomConn()

        def close(self) -> None:
            pass

    with TestClient(app) as client:
        # The lifespan startup assigns the real pool; override it inside the
        # client context so the teardown close() still runs against our stub.
        monkeypatch.setattr(app.state, "db_pool", _BoomPool())
        resp = client.get("/api/fleet/graph", params={"decay_lambda": 0.77})
    assert resp.status_code == 200
    body = resp.json()
    assert body["nodes"] == [] and body["edges"] == []
    assert body["stale"] is True, "a canceled query must be marked stale, not an empty fleet"
