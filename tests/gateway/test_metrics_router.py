"""GET /api/metrics HTTP integration tests.

The per-unit aggregation math is unit-tested in tests/shared/test_metrics.py
(pure functions over hand-built EventRow). This file locks the *endpoint
contract*: the windowed Loki fetch wires `attributes.X` keys to the real emit
field names, the `{meta, metrics}` envelope shape holds, and the `days` / `agent`
query params behave. The Loki backend is the in-memory `FakeLoki`
(monkeypatched onto `gateway.loki_events`); the `agents` table stays real SQL
(`/api/metrics/agents` reads labels from it).
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from tests.gateway.loki_fake import FakeLoki

_UNITS = {"syntax_fix", "exec", "llm_turns", "agent_activity", "sdk_usage"}


@pytest.fixture
def loki_fake(monkeypatch: pytest.MonkeyPatch) -> FakeLoki:
    fake = FakeLoki()
    monkeypatch.setattr("gateway.loki_events.count_events", fake.count_events)
    monkeypatch.setattr("gateway.loki_events.count_grouped", fake.count_grouped)
    monkeypatch.setattr("gateway.loki_events.query_events", fake.query_events)
    monkeypatch.setattr("gateway.loki_events.query_projected_lines", fake.query_projected_lines)
    return fake


def _insert_agent(db: psycopg.Connection, *, spawner: str = "user") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (f"agent-{spawner}",))
        row = cur.fetchone()
    assert row is not None, "INSERT ... RETURNING always yields a row"
    return row[0]


def _insert_event(
    fake: FakeLoki,
    *,
    event: str,
    agent_id: int | None = None,
    payload: dict[str, object] | None = None,
    ts_offset_days: float = 0,
) -> None:
    fake.add(
        event=event,
        agent_id=agent_id,
        payload=payload or {},
        ts_offset_hours=ts_offset_days * 24,
    )


def test_metrics_empty_db_returns_envelope_with_all_units(
    db_conn: psycopg.Connection, loki_fake: FakeLoki
) -> None:
    """Empty events table -> meta zeros, all four metric units present."""
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get("/api/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["total_events"] == 0
    assert body["meta"]["distinct_agents"] == 0
    assert body["meta"]["window_days"] == 1
    assert body["meta"]["agent_filter"] is None
    assert set(body["metrics"]) == _UNITS


def test_metrics_wires_payload_field_names(
    db_conn: psycopg.Connection, loki_fake: FakeLoki
) -> None:
    """A code+syntax_fix+exec+llm_usage thread aggregates through the right keys.

    Locks the Loki fetch -> unit-payload field-name contract end to end: if any
    emit site renames `fixes` / `body` / `in_total` / `cache_read` / `spawner`,
    or the idle-halt `body` sentinel, this reds.
    """
    aid = _insert_agent(db_conn)
    _insert_event(loki_fake, event="code", agent_id=aid, payload={"body": "print(1)"})
    _insert_event(
        loki_fake, event="syntax_fix", agent_id=aid, payload={"fixes": "ruff,ruff_format"}
    )
    _insert_event(loki_fake, event="exec", agent_id=aid, payload={"body": "1\n", "ok": True})
    _insert_event(
        loki_fake,
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 1000, "out_total": 200, "cache_read": 800, "model": "deepseek-v4-pro"},
    )
    # agent_activity reads `spawner` (split on ":") and the exact idle-halt body
    # sentinel — both are emit-site string contracts a unit test can't lock.
    _insert_event(loki_fake, event="agent_spawned", agent_id=aid, payload={"spawner": "agent:1"})
    _insert_event(loki_fake, event="halt", agent_id=aid, payload={"body": "no tool_call (idle)"})
    _insert_event(loki_fake, event="halt", agent_id=aid, payload={"body": "system_halt (compact)"})
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/metrics").json()
    m = body["metrics"]
    assert m["syntax_fix"]["trigger_counts"] == {"ruff": 1, "ruff_format": 1}
    assert m["exec"]["exec_ok"] == 1
    assert m["exec"]["exec_total"] == 1
    assert m["llm_turns"]["tokens_in"] == 1000
    assert m["llm_turns"]["tokens_cached"] == 800
    assert m["llm_turns"]["cache_hit_pct"] == 80.0
    # spawner "agent:1" buckets under "agent" and counts as a subagent; only the
    # exact idle sentinel is an idle halt (the compact halt is not).
    assert m["agent_activity"]["spawns_by_spawner"] == {"agent": 1}
    assert m["agent_activity"]["subagent_spawns"] == 1
    assert m["agent_activity"]["idle_halts"] == 1
    assert body["meta"]["distinct_agents"] == 1


def test_metrics_agent_filter_scopes_to_one(
    db_conn: psycopg.Connection, loki_fake: FakeLoki
) -> None:
    """`?agent=` restricts the fetch to a single agent_id."""
    a1 = _insert_agent(db_conn, spawner="one")
    a2 = _insert_agent(db_conn, spawner="two")
    _insert_event(loki_fake, event="code", agent_id=a1, payload={"body": "x"})
    _insert_event(loki_fake, event="code", agent_id=a2, payload={"body": "y"})
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/metrics?agent={a1}").json()
    assert body["meta"]["agent_filter"] == a1
    assert body["meta"]["distinct_agents"] == 1
    assert body["metrics"]["exec"]["code_len_chars"]["n"] == 1


def test_metrics_null_agent_events_not_counted(
    db_conn: psycopg.Connection, loki_fake: FakeLoki
) -> None:
    """Service-level events (agent_id None) count toward total_events but not
    distinct_agents (which filters None), and don't crash the per-agent grouping."""
    aid = _insert_agent(db_conn)
    _insert_event(loki_fake, event="code", agent_id=aid, payload={"body": "x"})
    _insert_event(loki_fake, event="log", agent_id=None, payload={})
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/metrics").json()
    assert body["meta"]["total_events"] == 2
    assert body["meta"]["distinct_agents"] == 1


def test_metrics_window_filters_old_rows(db_conn: psycopg.Connection, loki_fake: FakeLoki) -> None:
    """`days=1` drops events older than the window; `days=7` keeps them."""
    aid = _insert_agent(db_conn)
    _insert_event(loki_fake, event="code", agent_id=aid, payload={"body": "old"}, ts_offset_days=3)
    _insert_event(loki_fake, event="code", agent_id=aid, payload={"body": "new"}, ts_offset_days=0)
    db_conn.commit()
    with TestClient(app) as client:
        one_day = client.get("/api/metrics?days=1").json()
        seven_day = client.get("/api/metrics?days=7").json()
    assert one_day["meta"]["total_events"] == 1
    assert seven_day["meta"]["total_events"] == 2


def test_metrics_days_out_of_range_rejected(db_conn: psycopg.Connection) -> None:
    """`days` is capped at [1, 30] to bound the scan."""
    db_conn.commit()
    with TestClient(app) as client:
        assert client.get("/api/metrics?days=0").status_code == 422
        assert client.get("/api/metrics?days=31").status_code == 422


def test_metrics_since_compact_param(db_conn: psycopg.Connection, loki_fake: FakeLoki) -> None:
    """`?since_compact=true` drops each agent's pre-compact events (the compact
    halt row itself is kept — `ts >=` cutoff); default counts everything and
    echoes since_compact=false in meta."""
    aid = _insert_agent(db_conn)
    _insert_event(
        loki_fake,
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 1000, "out_total": 100, "cache_read": 0, "model": "deepseek-v4-pro"},
        ts_offset_days=0.5,
    )
    _insert_event(
        loki_fake,
        event="halt",
        agent_id=aid,
        payload={"body": "system_halt (compact)"},
        ts_offset_days=0.3,
    )
    _insert_event(
        loki_fake,
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 200, "out_total": 20, "cache_read": 0, "model": "deepseek-v4-pro"},
        ts_offset_days=0.1,
    )
    db_conn.commit()
    with TestClient(app) as client:
        full = client.get("/api/metrics").json()
        since = client.get("/api/metrics?since_compact=true").json()
    assert full["meta"]["since_compact"] is False
    assert full["metrics"]["llm_turns"]["llm_calls"] == 2
    assert full["metrics"]["llm_turns"]["tokens_in"] == 1200
    assert since["meta"]["since_compact"] is True
    assert since["metrics"]["llm_turns"]["llm_calls"] == 1
    assert since["metrics"]["llm_turns"]["tokens_in"] == 200
    # halt + post-compact llm_usage survive the filter
    assert since["meta"]["total_events"] == 2


# ── /api/metrics/agents ─────────────────────────────────────────────────────


def test_metrics_agents_empty(db_conn: psycopg.Connection, loki_fake: FakeLoki) -> None:
    """Empty events table -> empty agents list, meta zeros."""
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/metrics/agents?days=1").json()
    assert body["agents"] == []
    assert body["meta"]["total_events"] == 0
    assert body["meta"]["distinct_agents"] == 0
    assert body["meta"]["window_days"] == 1
    assert body["meta"]["since_compact"] is False


def test_metrics_agents_multi_agent(db_conn: psycopg.Connection, loki_fake: FakeLoki) -> None:
    """One row per agent with its own aggregates + label from the agents table,
    sorted by cost descending. Service-level (agent_id None) events count in
    meta.total_events but produce no row."""
    a1 = _insert_agent(db_conn, spawner="one")
    a2 = _insert_agent(db_conn, spawner="two")
    # a1: cheap — unpriced model, one ok turn, one failed exec
    _insert_event(
        loki_fake,
        event="llm_usage",
        agent_id=a1,
        payload={"in_total": 1000, "out_total": 100, "cache_read": 800, "model": "no-such-model"},
    )
    _insert_event(
        loki_fake, event="turn_end", agent_id=a1, payload={"duration_seconds": 1, "ok": True}
    )
    _insert_event(loki_fake, event="exec_failed", agent_id=a1, payload={"exc_type": "ValueError"})
    # a2: expensive — claude-opus-4-8 (5, 5, 25) USD/M: 1M in + 1M out = 30.0
    _insert_event(
        loki_fake,
        event="llm_usage",
        agent_id=a2,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
        },
    )
    _insert_event(loki_fake, event="exec", agent_id=a2, payload={"body": "ok"})
    _insert_event(loki_fake, event="log", agent_id=None, payload={})
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/metrics/agents?days=1").json()
    assert body["meta"]["total_events"] == 6
    assert body["meta"]["distinct_agents"] == 2
    assert [a["agent_id"] for a in body["agents"]] == [a2, a1]  # cost desc
    expensive, cheap = body["agents"]
    assert expensive["label"] == "agent-two"
    assert expensive["cost_usd"] == 30.0
    assert expensive["llm_calls"] == 1
    assert expensive["exec_ok"] == 1
    assert expensive["exec_failed"] == 0
    assert cheap["label"] == "agent-one"
    assert cheap["cost_usd"] == 0  # unpriced model contributes 0
    assert cheap["events"] == 3
    assert cheap["tokens_in"] == 1000
    assert cheap["cache_hit_pct"] == 80.0
    assert cheap["turn_ok"] == 1
    assert cheap["turn_total"] == 1
    assert cheap["exec_failed"] == 1


def test_metrics_agents_since_compact(db_conn: psycopg.Connection, loki_fake: FakeLoki) -> None:
    """`?since_compact=true` counts only each agent's events at or after its
    latest compact halt; an agent that never compacted keeps everything."""
    compacted = _insert_agent(db_conn, spawner="compacted")
    plain = _insert_agent(db_conn, spawner="plain")
    _insert_event(
        loki_fake,
        event="llm_usage",
        agent_id=compacted,
        payload={"in_total": 1000, "out_total": 100, "cache_read": 0, "model": "deepseek-v4-pro"},
        ts_offset_days=0.5,
    )
    _insert_event(
        loki_fake,
        event="halt",
        agent_id=compacted,
        payload={"body": "system_halt (compact)"},
        ts_offset_days=0.3,
    )
    _insert_event(
        loki_fake,
        event="llm_usage",
        agent_id=compacted,
        payload={"in_total": 200, "out_total": 20, "cache_read": 0, "model": "deepseek-v4-pro"},
        ts_offset_days=0.1,
    )
    _insert_event(
        loki_fake,
        event="llm_usage",
        agent_id=plain,
        payload={"in_total": 500, "out_total": 50, "cache_read": 0, "model": "deepseek-v4-pro"},
        ts_offset_days=0.5,
    )
    db_conn.commit()
    with TestClient(app) as client:
        full = client.get("/api/metrics/agents?days=1").json()
        since = client.get("/api/metrics/agents?days=1&since_compact=true").json()
    by_id = {a["agent_id"]: a for a in full["agents"]}
    assert by_id[compacted]["llm_calls"] == 2
    assert by_id[plain]["llm_calls"] == 1
    assert since["meta"]["since_compact"] is True
    by_id = {a["agent_id"]: a for a in since["agents"]}
    # compacted agent: only the post-compact call counted (+ the halt row itself)
    assert by_id[compacted]["llm_calls"] == 1
    assert by_id[compacted]["tokens_in"] == 200
    assert by_id[compacted]["events"] == 2
    # the never-compacted agent is untouched by the filter
    assert by_id[plain]["llm_calls"] == 1
