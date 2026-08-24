"""GET /api/fleet/graph integration tests.

FastAPI TestClient + real ava_test DB. The SQL is the one place a column-name /
cast / filter typo passes the frontend tests (which feed mock data) but breaks
live, so it is exercised against a real DB here. Covers:
- `total_tokens` — per-agent all-time in+out llm_usage counter sum, read from
  Prometheus via gateway/prom_metrics (mocked here; its own unit tests lock
  the PromQL text).
- `node_score` — windowed SUM(in)*0.1 + SUM(out)*1.0 (drives node size).
- edge weight — lineage (spawn/fork/resurrect) permanent COUNT*2.0 (no decay,
  always shown); message (send_message) recency-decayed, dropped below 0.01.
  Edges stitch the frozen PG archive (pre-cutover rows) with the Loki fake
  (live tail) — task #1280 interim, collapses to Loki-only after #1281.
- category negative samples — telemetry message rows never become edges; a
  NULL agent_id audit row never 500s the endpoint.
"""

import math
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors as pg_errors

from gateway import loki_events, prom_metrics, telemetry_staleness
from gateway.app import app
from gateway.routers import fleet_graph
from shared.cluster import home_label
from shared.paths import ava_home
from tests.gateway.loki_fake import FakeLoki


def _seed_agent(db_conn: psycopg.Connection, *, status: str = "running") -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', %s)",
            (new_id, status),
        )
    db_conn.commit()
    return new_id


# The metric names fleet_graph reads (must match the OTLP-mapped counters).
_IN_METRIC = "ava_llm_usage_in_total"
_OUT_METRIC = "ava_llm_usage_out_total"


def _fresh_heartbeat_age(*, timeout_s: float | None = None) -> float:
    del timeout_s
    return 30.0


@pytest.fixture(autouse=True)
def fake_loki(monkeypatch: pytest.MonkeyPatch) -> FakeLoki:
    """Route all loki_events calls through an in-memory fake; each test gets
    an empty store and adds its own rows."""
    fake = FakeLoki()
    monkeypatch.setattr(loki_events, "query_events", fake.query_events)
    monkeypatch.setattr(loki_events, "count_events", fake.count_events)
    monkeypatch.setattr(loki_events, "attribute_aggregate", fake.attribute_aggregate)
    return fake


@pytest.fixture(autouse=True)
def _mock_prom(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI has no Prometheus — every fleet_graph test fakes
    prom_metrics.sum_by; the default fake has no series (tokens 0 / score 0).
    Tests that need token values re-install a richer fake over this one (it
    runs first, the per-test install wins)."""

    def fake_sum_by(
        metric: str, by: str, *, window: timedelta | None = None, timeout_s: float | None = None
    ) -> dict[str, float]:
        return {}

    monkeypatch.setattr(prom_metrics, "sum_by", fake_sum_by)


@pytest.fixture(autouse=True)
def _fresh_telemetry_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing route tests describe fresh-source behavior."""
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


def _install_prom(
    monkeypatch: pytest.MonkeyPatch,
    *,
    all_time: dict[str, dict[str, float]] | None = None,
    windowed: dict[str, dict[str, float]] | None = None,
) -> None:
    """Fake gateway.prom_metrics.sum_by: `all_time` maps metric ->
    {agent_id: value} for window=None calls, `windowed` for windowed
    calls. A metric absent from both maps reads as {} (no llm_usage series)."""

    def fake_sum_by(
        metric: str, by: str, *, window: timedelta | None = None, timeout_s: float | None = None
    ) -> dict[str, float]:
        src = windowed if window is not None else all_time
        return (src or {}).get(metric, {})

    monkeypatch.setattr(prom_metrics, "sum_by", fake_sum_by)


def _event(
    db_conn: psycopg.Connection,
    *,
    source_agent: int,
    target_agent: int,
    event_type: str,
    age_hours: float = 0.0,
) -> None:
    """Insert one ARCHIVE-era audit event (a directed inter-agent operation)
    into the frozen PG `events` archive, optionally aged `age_hours` into the
    past. Rows must sit below the boundary anchor (max(events.ts) — see
    `_archive_boundary_anchor`) to be served by the archive side."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events "
            "(ts, agent_id, event_name, source, target_agent_id, machine, process, category, level) "
            "VALUES (now() - %s * interval '1 hour', %s, %s, 'test', %s, "
            "'test', 'test', 'audit', 'info')",
            (age_hours, source_agent, event_type, target_agent),
        )
    db_conn.commit()


def _archive_boundary_anchor(db_conn: psycopg.Connection) -> None:
    """Pin the archive's freeze point to ~now: the graph partitions the
    timeline at max(events.ts), so a test mixing archive-era rows (old ts)
    with Loki rows (ts≈now) must insert a boundary anchor — otherwise the
    oldest archive row would BE the boundary and sit outside its own window
    (`ts < boundary`). A telemetry turn_end row serves as the freeze marker
    (non-audit: it never forms an edge)."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events "
            "(ts, agent_id, event_name, source, machine, process, category, level) "
            "VALUES (now() - interval '6 minutes', NULL, 'turn_end', 'test', "
            "'test', 'test', 'telemetry', 'info')"
        )
    db_conn.commit()


def _event_loki(
    fake_loki: FakeLoki,
    *,
    source_agent: int,
    target_agent: int,
    event_type: str,
    ts_offset_hours: float = 0.0,
) -> None:
    """Add one LIVE-era audit event to the Loki fake (the post-cutover tail)."""
    fake_loki.add(
        event=event_type,
        agent_id=source_agent,
        target_agent_id=target_agent,
        ts_offset_hours=ts_offset_hours,
        category="audit",
    )


def test_loki_edge_tail_is_scoped_to_this_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def query_events(**kwargs: object) -> tuple[list[dict[str, object]], bool]:
        calls.append(kwargs)
        return [], False

    monkeypatch.setattr(loki_events, "query_events", query_events)

    fleet_graph._fetch_loki_edges(
        boundary=None,
        now=datetime(2026, 8, 24, tzinfo=UTC),
    )

    assert calls[0]["cluster"] == home_label(ava_home())


def test_loki_edge_tail_keeps_unlabeled_history_and_excludes_other_cluster(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """A pre-labeling edge is local history; a labeled foreign edge is not."""
    source = _seed_agent(db_conn)
    target = _seed_agent(db_conn)
    _archive_boundary_anchor(db_conn)
    _event_loki(fake_loki, source_agent=source, target_agent=target, event_type="spawn")
    _event_loki(fake_loki, source_agent=source, target_agent=target, event_type="spawn")
    fake_loki.rows[-1]["cluster"] = "other-cluster"

    with TestClient(app) as client:
        response = client.get("/api/fleet/graph")

    assert response.status_code == 200
    edges = response.json()["edges"]
    assert len(edges) == 1
    assert edges[0]["from_agent"] == target
    assert edges[0]["to_agent"] == source
    assert edges[0]["event_type"] == "spawn"
    assert edges[0]["weight"] == 2.0
    assert edges[0]["event_count"] == 1
    assert edges[0]["last_seen_at"]


def _nodes_by_id(client: TestClient, query: str = "") -> dict[int, dict]:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    resp = client.get(f"/api/fleet/graph{query}")
    assert resp.status_code == 200, resp.text
    return {n["agent_id"]: n for n in resp.json()["nodes"]}  # pyright: ignore[reportUnknownVariableType]


def test_total_tokens_sums_in_plus_out_counters(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _seed_agent(db_conn)
    _install_prom(monkeypatch, all_time={_IN_METRIC: {str(a): 300.0}, _OUT_METRIC: {str(a): 80.0}})

    with TestClient(app) as client:
        nodes = _nodes_by_id(client)  # pyright: ignore[reportUnknownVariableType]

    assert nodes[a]["total_tokens"] == 380  # in 300 + out 80


def test_node_exposes_canonical_status_and_independent_liveness(
    db_conn: psycopg.Connection,
) -> None:
    a = _seed_agent(db_conn, status="restarting")
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET liveness_state = 'offline' WHERE id = %s",
            (a,),
        )
    db_conn.commit()

    with TestClient(app) as client:
        nodes = _nodes_by_id(client)  # pyright: ignore[reportUnknownVariableType]

    assert nodes[a]["status"] == "restarting"
    assert nodes[a]["liveness_state"] == "offline"


def test_total_tokens_zero_without_usage(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _seed_agent(db_conn)
    _install_prom(monkeypatch)  # no llm_usage series -> all counters absent

    with TestClient(app) as client:
        nodes = _nodes_by_id(client)  # pyright: ignore[reportUnknownVariableType]
    assert nodes[a]["total_tokens"] == 0


def test_total_tokens_comes_from_llm_usage_counters_only(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Prometheus side only ever sees llm_usage-derived counters (the
    OTLP mapper emits ava_llm_usage_* for llm_usage events alone), so a
    turn_end-style payload can never leak into token totals — lock the mock
    contract: in+out counters are the only input."""
    a = _seed_agent(db_conn)
    _install_prom(monkeypatch, all_time={_IN_METRIC: {str(a): 100.0}, _OUT_METRIC: {str(a): 50.0}})

    with TestClient(app) as client:
        nodes = _nodes_by_id(client)  # pyright: ignore[reportUnknownVariableType]

    assert nodes[a]["total_tokens"] == 150


def test_total_tokens_scoped_per_agent(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _install_prom(
        monkeypatch,
        all_time={
            _IN_METRIC: {str(a): 100.0, str(b): 1.0},
            _OUT_METRIC: {str(a): 50.0, str(b): 1.0},
        },
    )

    with TestClient(app) as client:
        nodes = _nodes_by_id(client)  # pyright: ignore[reportUnknownVariableType]

    assert nodes[a]["total_tokens"] == 150
    assert nodes[b]["total_tokens"] == 2


# ── node_score: windowed weighted token work ──────────────────────────────


def test_node_score_weights_output_ten_times_input(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _seed_agent(db_conn)
    _install_prom(
        monkeypatch,
        all_time={_IN_METRIC: {str(a): 300.0}, _OUT_METRIC: {str(a): 80.0}},
        windowed={_IN_METRIC: {str(a): 300.0}, _OUT_METRIC: {str(a): 80.0}},
    )

    with TestClient(app) as client:
        nodes = _nodes_by_id(client)  # pyright: ignore[reportUnknownVariableType]

    # SUM(in)*0.1 + SUM(out)*1.0 = 300*0.1 + 80*1.0 = 30 + 80 = 110
    assert nodes[a]["node_score"] == 110.0


def test_node_score_zero_without_usage(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _seed_agent(db_conn)
    _install_prom(monkeypatch)  # no llm_usage series -> score 0

    with TestClient(app) as client:
        nodes = _nodes_by_id(client)  # pyright: ignore[reportUnknownVariableType]
    assert nodes[a]["node_score"] == 0.0


def test_node_score_windowed_excludes_old_events(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _seed_agent(db_conn)
    # all-time counters carry the old + recent increments; the 24h window
    # (increase over [24h]) only the recent one — the Prometheus side applies
    # the window, the route just merges the two views.
    _install_prom(
        monkeypatch,
        all_time={_IN_METRIC: {str(a): 100.0 + 999.0}, _OUT_METRIC: {str(a): 100.0 + 999.0}},
        windowed={_IN_METRIC: {str(a): 100.0}, _OUT_METRIC: {str(a): 100.0}},
    )

    with TestClient(app) as client:
        nodes = _nodes_by_id(client, "?hours=24")  # pyright: ignore[reportUnknownVariableType]

    # Only the recent event scores: 100*0.1 + 100*1.0 = 110. total_tokens stays
    # all-time (both events).
    assert nodes[a]["node_score"] == 110.0
    assert nodes[a]["total_tokens"] == 100 + 100 + 999 + 999


def test_node_drops_degree_fields(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    with TestClient(app) as client:
        nodes = _nodes_by_id(client)  # pyright: ignore[reportUnknownVariableType]
    assert "degree_in" not in nodes[a]
    assert "degree_out" not in nodes[a]
    assert "node_score" in nodes[a]


# ── edge weight: per-event sum-of-exponentials decay ──────────────────────


def _edges_by_type(client: TestClient, query: str = "") -> dict[str, dict]:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    resp = client.get(f"/api/fleet/graph{query}")
    assert resp.status_code == 200, resp.text
    return {e["event_type"]: e for e in resp.json()["edges"]}  # pyright: ignore[reportUnknownVariableType]


def test_edge_weight_type_multiplier_fresh(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    _event_loki(fake_loki, source_agent=s, target_agent=c, event_type="spawn")
    _event_loki(fake_loki, source_agent=s, target_agent=c, event_type="send_message")

    with TestClient(app) as client:
        edges = _edges_by_type(client)  # pyright: ignore[reportUnknownVariableType]

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
    _archive_boundary_anchor(db_conn)

    with TestClient(app) as client:
        edges = _edges_by_type(client)  # pyright: ignore[reportUnknownVariableType]

    assert edges["spawn"]["weight"] == 2.0
    assert edges["spawn"]["event_count"] == 1


def test_resurrect_edge_included_as_permanent_lineage(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # resurrect is a lineage tie now included in the graph (it was missing before).
    _event_loki(fake_loki, source_agent=s, target_agent=c, event_type="resurrect")
    _event_loki(fake_loki, source_agent=s, target_agent=c, event_type="resurrect")

    with TestClient(app) as client:
        edges = _edges_by_type(client)  # pyright: ignore[reportUnknownVariableType]

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
    _archive_boundary_anchor(db_conn)

    with TestClient(app) as client:
        edges = _edges_by_type(client, "?hours=24")  # pyright: ignore[reportUnknownVariableType]

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
    _archive_boundary_anchor(db_conn)

    with TestClient(app) as client:
        edges = _edges_by_type(client)  # pyright: ignore[reportUnknownVariableType]

    assert "send_message" not in edges


def test_edge_weight_sums_per_event_with_decay(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # Two send_message events straddling the cutover: one fresh (Loki side),
    # one 48h (2 days) old (archive side). The merged weight sums both;
    # weight = (EXP(0) + EXP(-0.5*2)) * 1.0.
    _event_loki(fake_loki, source_agent=s, target_agent=c, event_type="send_message")
    _event(db_conn, source_agent=s, target_agent=c, event_type="send_message", age_hours=48)
    _archive_boundary_anchor(db_conn)

    with TestClient(app) as client:
        edges = _edges_by_type(client)  # pyright: ignore[reportUnknownVariableType]

    expected = 1.0 + math.exp(-0.5 * 2.0)
    assert edges["send_message"]["weight"] == pytest.approx(expected, abs=1e-3)  # pyright: ignore[reportUnknownMemberType]
    assert edges["send_message"]["event_count"] == 2


def test_edge_window_excludes_old_events(db_conn: psycopg.Connection) -> None:
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    _event(db_conn, source_agent=s, target_agent=c, event_type="send_message", age_hours=100)
    _archive_boundary_anchor(db_conn)

    with TestClient(app) as client:
        # 24h window excludes the 100h-old event -> no edges.
        assert _edges_by_type(client, "?hours=24") == {}
        # All-time still sees it.
        assert "send_message" in _edges_by_type(client)


def test_decay_lambda_param_steepens_decay(db_conn: psycopg.Connection) -> None:
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    _event(db_conn, source_agent=s, target_agent=c, event_type="send_message", age_hours=48)
    _archive_boundary_anchor(db_conn)

    with TestClient(app) as client:
        gentle = _edges_by_type(client, "?decay_lambda=0.1")["send_message"]["weight"]  # pyright: ignore[reportUnknownVariableType]
        steep = _edges_by_type(client, "?decay_lambda=2.0")["send_message"]["weight"]  # pyright: ignore[reportUnknownVariableType]

    # A larger lambda decays an aged event harder -> smaller weight.
    assert steep < gentle


# ── terminated endpoint filtering (SQL layer) ──────────────────────────


def test_edges_touching_terminated_agent_excluded_by_default(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """The default graph excludes terminated agents; an edge that touches one
    can never be drawn (its endpoint is not in the node set). The SQL layer
    filters it so the payload only carries drawable edges."""
    live = _seed_agent(db_conn)
    dead = _seed_agent(db_conn, status="terminated")
    _event_loki(fake_loki, source_agent=live, target_agent=dead, event_type="spawn")
    _event_loki(fake_loki, source_agent=dead, target_agent=live, event_type="send_message")

    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")
    assert resp.status_code == 200
    body = resp.json()
    assert {n["agent_id"] for n in body["nodes"]} == {live}
    assert body["edges"] == []


def test_hibernating_node_with_terminated_spawner_shows_isolated(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """Task #1089/#1104 regression — the #2753 shape: a live (hibernating)
    agent whose spawner has since terminated. The live node renders on its
    own; the terminated partner is NOT a node and the spawn edge is NOT
    returned (user ruling 2026-08-09: terminated agents never appear in the
    graph; a live node with no live parent simply shows without the edge)."""
    hibernating = _seed_agent(db_conn, status="hibernating")
    dead = _seed_agent(db_conn, status="terminated")
    _event_loki(fake_loki, source_agent=dead, target_agent=hibernating, event_type="spawn")

    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")
    assert resp.status_code == 200
    body = resp.json()
    assert {n["agent_id"] for n in body["nodes"]} == {hibernating}
    assert body["edges"] == []


def test_edge_between_two_terminated_agents_excluded_by_default(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    d1 = _seed_agent(db_conn, status="terminated")
    d2 = _seed_agent(db_conn, status="terminated")
    _event_loki(fake_loki, source_agent=d1, target_agent=d2, event_type="spawn")

    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")
    assert resp.status_code == 200
    assert resp.json()["edges"] == []


def test_include_terminated_returns_terminated_endpoint_edges(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """?include_terminated=true restores the full edge set (lineage archive
    mode) — the filter is the same switch that governs the node set."""
    live = _seed_agent(db_conn)
    dead = _seed_agent(db_conn, status="terminated")
    _event_loki(fake_loki, source_agent=live, target_agent=dead, event_type="spawn")

    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph?include_terminated=true")
    assert resp.status_code == 200
    body = resp.json()
    assert {n["agent_id"] for n in body["nodes"]} == {live, dead}
    assert len(body["edges"]) == 1
    assert body["edges"][0]["event_type"] == "spawn"


def test_live_live_edge_still_returned_after_filter(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """The filter only drops terminated endpoints — a live-live edge must
    survive unchanged (weight semantics untouched)."""
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    _event_loki(fake_loki, source_agent=s, target_agent=c, event_type="spawn")
    _event_loki(fake_loki, source_agent=s, target_agent=c, event_type="send_message")

    with TestClient(app) as client:
        edges = _edges_by_type(client)  # pyright: ignore[reportUnknownVariableType]

    assert edges["spawn"]["weight"] == 2.0
    assert edges["send_message"]["weight"] == 1.0


# ── category 负样本: audit 专属边, telemetry 不得成边 ─────────────────────


def test_telemetry_send_message_does_not_become_edge(db_conn: psycopg.Connection) -> None:
    """A send_message row with category='telemetry' must NOT become a graph
    edge — the edge query filters category='audit', so an accidental telemetry
    write of a message-shaped event stays invisible to the graph."""
    s = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # category='telemetry' (the _llm_usage-style category), NOT audit.
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events "
            "(agent_id, event_name, source, target_agent_id, machine, process, category, level) "
            "VALUES (%s, 'send_message', 'test', %s, 'test', 'test', 'telemetry', 'info')",
            (s, c),
        )
    db_conn.commit()

    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")
    assert resp.status_code == 200
    assert resp.json()["edges"] == []


def test_null_agent_id_audit_row_does_not_500_and_makes_no_edge(
    db_conn: psycopg.Connection,
) -> None:
    """An audit row whose agent_id is NULL (service-level event — the W9
    telemetry change first allowed such rows to land) must not crash the
    graph endpoint: the edge query filters agent_id IS NOT NULL, so the
    NULL to_agent never reaches the pydantic int field."""
    t = _seed_agent(db_conn)  # target side is a real agent
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events "
            "(ts, agent_id, event_name, source, target_agent_id, machine, process, category, level) "
            "VALUES (now(), NULL, 'send_message', 'system', %s, "
            "'test', 'test', 'audit', 'info')",
            (t,),
        )
    db_conn.commit()

    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")
    assert resp.status_code == 200
    assert resp.json()["edges"] == []


# ── Redis cache: 60s TTL, keyed by params, fail-open ──────────────────────


def test_cache_serves_stale_graph_within_ttl(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second request within the 60s TTL hits the Redis cache and does not
    re-query Prometheus: change the mocked counters after the first request
    and assert the response still carries the first request's data."""
    a = _seed_agent(db_conn)
    _install_prom(monkeypatch, all_time={_IN_METRIC: {str(a): 100.0}, _OUT_METRIC: {str(a): 50.0}})

    with TestClient(app) as client:
        first = _nodes_by_id(client)  # pyright: ignore[reportUnknownVariableType]

    # Counter values change after the first request — must NOT be visible.
    _install_prom(
        monkeypatch, all_time={_IN_METRIC: {str(a): 9999.0}, _OUT_METRIC: {str(a): 9999.0}}
    )

    with TestClient(app) as client:
        second = _nodes_by_id(client)  # pyright: ignore[reportUnknownVariableType]

    assert first[a]["total_tokens"] == 150
    assert second[a]["total_tokens"] == 150  # cached, not 19998


def test_cache_key_separates_params(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Different query params get different cache keys: a 24h-window request
    must not serve the all-time response (nor vice versa)."""
    a = _seed_agent(db_conn)
    _install_prom(
        monkeypatch,
        all_time={_IN_METRIC: {str(a): 200.0}, _OUT_METRIC: {str(a): 150.0}},
        windowed={_IN_METRIC: {str(a): 100.0}, _OUT_METRIC: {str(a): 50.0}},
    )

    with TestClient(app) as client:
        all_time = _nodes_by_id(client)  # pyright: ignore[reportUnknownVariableType]
        windowed = _nodes_by_id(client, "?hours=24")  # pyright: ignore[reportUnknownVariableType]

    # All-time includes the old increment; the 24h window excludes it.
    assert all_time[a]["total_tokens"] == 350
    assert all_time[a]["node_score"] == 170.0  # (200)*0.1 + (150)*1.0
    assert windowed[a]["node_score"] == 60.0  # only the fresh increment: 100*0.1 + 50*1.0


def test_cache_fail_open_when_redis_down(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Redis outage (sync_redis raising) degrades to a direct DB query —
    never a 500."""
    a = _seed_agent(db_conn)
    _install_prom(monkeypatch, all_time={_IN_METRIC: {str(a): 100.0}, _OUT_METRIC: {str(a): 50.0}})

    import gateway.routers.fleet_graph as fg

    def boom(*args: object, **kwargs: object) -> object:
        raise ConnectionError("redis down")

    monkeypatch.setattr(fg, "sync_redis", boom)
    with TestClient(app) as client:
        nodes = _nodes_by_id(client)  # pyright: ignore[reportUnknownVariableType]
    assert nodes[a]["total_tokens"] == 150


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
        "truncated": False,
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


# ── Prometheus outage: same visible degradation (R4 layer 2) ───────────────


def test_loki_down_degrades_to_stale_node_graph(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Loki outage (httpx transport error on the edge stream) returns an
    edge-less graph with the fetched nodes marked stale — never a silent zero
    (edges would otherwise vanish without a trace the moment Loki is
    unreachable)."""
    a = _seed_agent(db_conn)

    def boom(*args: object, **kwargs: object) -> object:
        raise httpx.ConnectError("loki unreachable")

    monkeypatch.setattr(loki_events, "query_events", boom)
    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")
    assert resp.status_code == 200
    body = resp.json()
    assert {node["agent_id"] for node in body["nodes"]} == {a}
    assert body["edges"] == []
    assert body["stale"] is True


def test_prometheus_down_degrades_to_stale_pg_node_graph(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Prometheus outage (httpx transport error on the aggregate query)
    returns the PG node set marked stale — never an empty fleet (the graph
    would otherwise render no node identities at all)."""
    a = _seed_agent(db_conn)

    def boom(
        metric: str,
        by: str,
        *,
        window: timedelta | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, float]:
        raise httpx.ConnectError("prometheus down")

    monkeypatch.setattr(prom_metrics, "sum_by", boom)
    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")
    assert resp.status_code == 200
    body = resp.json()
    assert {node["agent_id"] for node in body["nodes"]} == {a}
    assert body["edges"] == []
    assert body["stale"] is True
