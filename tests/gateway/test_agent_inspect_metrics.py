"""Integration tests for GET /api/agents/{id}/inspect/metrics (W13b).

The inspector surface of the plugin metric system: the gateway reads the
registry snapshot ($AVA_HOME/state/plugin_metrics.json — written by
scripts/gen_plugin_dashboard.py, mocked here by writing the file directly),
keeps `output`-inspector metrics, renders each template for the agent,
re-validates the rendered SQL, substitutes the Grafana time macros with a
fixed window, and executes the query against the test DB.

Locks: missing file -> [], unknown agent -> 404, malformed file / wrong
schema_version / invalid spec row / tampered template -> 500 with the reason,
{{agent_id}} template rendered with no agent id -> 400, execution-time query
failure -> per-metric `error` while the other metrics still render, and the
execution semantics (event_name/category literals, per-agent filtering, stat vs
series payloads, macro translation).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.routers._plugin_metrics import (
    _plugin_metrics_state_path,
    _render_metric_query,
    _translate_macros,
)
from shared.plugin_metrics import MetricSpec

# A valid inspector template — the fleet demo shape: per-agent task done rate.
_DEMO_QUERY = (
    "SELECT $__timeGroup(ts, $__interval) AS time, "
    "100.0 * count(*) FILTER (WHERE attributes->>'status' = 'done') "
    '/ NULLIF(count(*), 0) AS "done %" '
    "FROM events "
    "WHERE event_name = {event_name} AND category = {category} "
    "AND attributes ? 'status' AND {{agent_id}} AND $__timeFilter(ts) "
    "GROUP BY 1 ORDER BY 1"
)

# A stat-shaped inspector template (no time macro, one aggregate row).
_STAT_QUERY = (
    "SELECT count(*) AS fixes "
    "FROM events "
    "WHERE event_name = {event_name} AND category = {category} AND $__timeFilter(ts)"
)


def _metric(
    name: str = "demo_task_done_rate",
    *,
    event_name: str = "task_update",
    category: str = "audit",
    panel: str = "timeseries",
    query: str = _DEMO_QUERY,
    output: list[str] | None = None,
    unit: str = "percent",
) -> dict[str, Any]:
    """A registry-row dict (the shape export_registry writes)."""
    return {
        "name": name,
        "title": "Task done rate",
        "description": "test metric",
        "event_name": event_name,
        "category": category,
        "unit": unit,
        "panel": panel,
        "query": query,
        "output": output or ["inspector"],
        "plugin": "test_plugin",
    }


def _write_registry(*metrics: dict[str, Any]) -> Path:
    """Write the registry snapshot where the gateway reads it (the test
    AVA_HOME). Returns the path."""
    path = _plugin_metrics_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "exported_at": datetime.now(UTC).isoformat(),
                "metrics": list(metrics),
            }
        )
        + "\n"
    )
    return path


@pytest.fixture(autouse=True)
def _clean_metrics_state() -> Iterator[None]:
    """Every test starts without a registry file; tests that need one write it."""
    path = _plugin_metrics_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    yield
    path.unlink(missing_ok=True)


def _insert_agent(db: psycopg.Connection, label: str = "t") -> int:
    """INSERT an agents row + its agents_meta row (the /inspect family checks
    agents_meta; a bare `agents` row would 404)."""
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (label,))
        row = cur.fetchone()
    assert row is not None
    tid = row[0]
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (tid,),
        )
    return tid


def _insert_event(
    db: psycopg.Connection,
    *,
    agent_id: int,
    event_name: str = "task_update",
    category: str = "audit",
    payload: dict | None = None,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    ts_offset_hours: float = 0,
) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO events (ts, agent_id, level, event_name, attributes, machine, process, category, source) "
            "VALUES (now() - %s * interval '1 hour', %s, 'info', %s, %s::jsonb, "
            "'test', 'test', %s, 'test')",
            (ts_offset_hours, agent_id, event_name, json.dumps(payload or {}), category),
        )


# ── file absent / agent unknown ───────────────────────────────────────────────


def test_metrics_missing_registry_returns_empty(db_conn: psycopg.Connection) -> None:
    """No registry file (generator never ran) -> 200 []."""
    aid = _insert_agent(db_conn)
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 200
    assert resp.json() == []


def test_metrics_unknown_agent_404(db_conn: psycopg.Connection) -> None:
    """No agents_meta row -> 404 (same contract as /inspect)."""
    _write_registry(_metric())
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get("/api/agents/999999/inspect/metrics")
    assert resp.status_code == 404


# ── filtering + rendering + execution ─────────────────────────────────────────


def test_metrics_filters_inspector_output(db_conn: psycopg.Connection) -> None:
    """Only metrics whose output includes 'inspector' are returned; a
    grafana-only metric (even one sharing the same table shape) is skipped."""
    aid = _insert_agent(db_conn)
    _write_registry(
        _metric(name="insp_metric"),
        _metric(
            name="grafana_only",
            query="SELECT count(*) FROM events WHERE event_name = {event_name} AND category = {category}",
            output=["grafana"],
        ),
    )
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert [m["name"] for m in body] == ["insp_metric"]


def test_metrics_stat_scalar(db_conn: psycopg.Connection) -> None:
    """A stat metric returns the single aggregate as `value` (windowed to the
    inspector's 24h, so an old event falls out)."""
    aid = _insert_agent(db_conn)
    _write_registry(_metric(name="stat_count", panel="stat", query=_STAT_QUERY))
    _insert_event(db_conn, agent_id=aid, payload={"fixes": 1})
    _insert_event(db_conn, agent_id=aid, payload={"fixes": 1})
    _insert_event(db_conn, agent_id=aid, payload={"fixes": 1}, ts_offset_hours=48)
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["name"] == "stat_count"
    assert body[0]["panel"] == "stat"
    assert body[0]["value"] == 2  # 48h-old event outside the 24h window
    assert body[0]["series"] == []
    assert body[0]["error"] is None


def test_metrics_timeseries_series(db_conn: psycopg.Connection) -> None:
    """A timeseries metric returns a chronological series of (ts, value)
    points over the fixed 24h window."""
    aid = _insert_agent(db_conn)
    _write_registry(_metric())
    for _ in range(3):
        _insert_event(db_conn, agent_id=aid, payload={"status": "done"})
    _insert_event(db_conn, agent_id=aid, payload={"status": "pending"})
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    m = body[0]
    assert m["panel"] == "timeseries"
    assert m["unit"] == "percent"
    assert m["error"] is None
    assert m["value"] is None
    # 4 events in one hour -> one 1h bucket; done rate = 3/4 = 75.0
    assert len(m["series"]) == 1
    point = m["series"][0]
    assert point["value"] == 75.0
    # ts parses as an ISO instant (datetime serialized by FastAPI)
    datetime.fromisoformat(point["ts"])


def test_metrics_agent_id_placeholder_filters(db_conn: psycopg.Connection) -> None:
    """{{agent_id}} renders to `agent_id = <n>`: the metric counts only the
    requested agent's events, not the fleet's."""
    a1 = _insert_agent(db_conn)
    a2 = _insert_agent(db_conn)
    _write_registry(_metric())
    _insert_event(db_conn, agent_id=a1, payload={"status": "done"})
    _insert_event(db_conn, agent_id=a2, payload={"status": "done"})
    _insert_event(db_conn, agent_id=a2, payload={"status": "done"})
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{a1}/inspect/metrics")
        assert resp.status_code == 200
        m = resp.json()[0]
        # one 1h bucket, 1 done / 1 with status = 100.0
        assert m["series"][0]["value"] == 100.0
        resp2 = client.get(f"/api/agents/{a2}/inspect/metrics")
        assert resp2.status_code == 200
        assert resp2.json()[0]["series"][0]["value"] == 100.0


def test_metrics_macro_translation_unit() -> None:
    """The Grafana macros translate to the fixed inspector window; macro
    arguments (file-controlled) are consumed wholesale and never reach the
    output."""
    sql = _translate_macros(
        "SELECT $__timeGroup(ts, $__interval) AS time, count(*) "
        "FROM events WHERE event_name = 'k' AND $__timeFilter(ts) "
        "AND $__timeFilter(ts, 'extra')"
    )
    assert "date_trunc('hour', ts)" in sql
    assert "ts >= now() - interval '24 hours'" in sql
    assert "$__" not in sql
    # a weird argument inside the macro parens is discarded, not echoed
    sql2 = _translate_macros(
        "SELECT count(*) FROM events WHERE $__timeFilter(attributes->>'model')"
    )
    assert "attributes" not in sql2
    assert sql2.count("$__timeFilter") == 0


# ── safety re-validation (tampered file) ──────────────────────────────────────


@pytest.mark.parametrize(
    "tampered",
    [
        # DML sneaked past the generator
        "SELECT count(*) FROM events; DELETE FROM events",
        # different FROM target
        "SELECT count(*) FROM agents WHERE id = 1",
        # denied function call
        "SELECT pg_sleep(1) FROM events",
        # comment-injected
        "SELECT count(*) FROM events -- WHERE event_name = 'x'",
    ],
)
def test_metrics_tampered_query_500(db_conn: psycopg.Connection, tampered: str) -> None:
    """A registry file edited after generation fails the second validation ->
    500 with the reason (never executed)."""
    aid = _insert_agent(db_conn)
    _write_registry(
        _metric(name="tampered", query=tampered),
        _metric(name="still_fine"),
    )
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 500
    assert "re-validation" in resp.json()["detail"]


def test_metrics_malformed_registry_500(db_conn: psycopg.Connection) -> None:
    """Unparseable JSON -> 500 with the reason."""
    aid = _insert_agent(db_conn)
    path = _plugin_metrics_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 500
    assert "unreadable" in resp.json()["detail"]


def test_metrics_wrong_schema_version_500(db_conn: psycopg.Connection) -> None:
    """A registry written by a newer generator (schema_version=2) is refused
    loudly instead of guessed at."""
    aid = _insert_agent(db_conn)
    path = _plugin_metrics_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 2, "metrics": []}))
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 500
    assert "schema_version" in resp.json()["detail"]


def test_metrics_invalid_spec_row_500(db_conn: psycopg.Connection) -> None:
    """A registry row that is not a valid MetricSpec -> 500 (missing required
    field)."""
    aid = _insert_agent(db_conn)
    _write_registry({"name": "missing_fields"})
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 500
    assert "invalid metric spec" in resp.json()["detail"]


def test_metrics_render_requires_agent_id_400() -> None:
    """A {{agent_id}} template rendered without an agent id is refused with
    400. (The HTTP route always carries the id in the path, so this is a
    helper-level guard — defense in depth against a future caller.)"""
    spec = MetricSpec.model_validate(_metric())
    with pytest.raises(HTTPException) as exc_info:
        _render_metric_query(spec, agent_id=None)
    assert exc_info.value.status_code == 400


def test_metrics_render_substitutes_placeholders() -> None:
    """{event_name}/{category} become single-quoted literals; {{agent_id}}
    becomes `agent_id = <n>`; the rendered SQL passes the whitelist."""
    spec = MetricSpec.model_validate(_metric())
    query = _render_metric_query(spec, agent_id=7)
    assert "event_name = 'task_update'" in query
    assert "category = 'audit'" in query
    assert "agent_id = 7" in query
    assert "{{agent_id}}" not in query


# ── execution-time failure is per-metric, not fatal ───────────────────────────


def test_metrics_runtime_query_error_per_metric(
    db_conn: psycopg.Connection,
) -> None:
    """A template that passes the whitelist but fails at runtime (missing
    column) lands in that metric's `error`; the sibling metric still renders
    and the request stays 200."""
    aid = _insert_agent(db_conn)
    _write_registry(
        _metric(name="broken", query="SELECT count(*) FROM events WHERE unknown_col = 1"),
        _metric(name="healthy", panel="stat", query=_STAT_QUERY),
    )
    _insert_event(db_conn, agent_id=aid, payload={"status": "done"})
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 200
    by_name = {m["name"]: m for m in resp.json()}
    assert "column" in by_name["broken"]["error"].lower()
    assert by_name["healthy"]["error"] is None
    assert by_name["healthy"]["value"] == 1


def test_metrics_read_only_is_transaction_scoped(
    db_conn: psycopg.Connection,
) -> None:
    """The read-only enforcement must NOT leak onto pooled connections.

    The endpoint opens its transaction with `SET TRANSACTION READ ONLY`
    (server-enforced, transaction-scoped) instead of `Connection.read_only` —
    that attribute is client-side state that persists on the pooled
    connection object after return, so a later borrower could inherit a
    read-only session and its writes would fail. After a metrics request,
    the gateway's own pool must still accept writes."""
    aid = _insert_agent(db_conn)
    _write_registry(_metric(name="stat_count", panel="stat", query=_STAT_QUERY))
    _insert_event(db_conn, agent_id=aid, payload={"status": "done"})
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
        assert resp.status_code == 200
        assert resp.json()[0]["value"] == 1
        # Same pool the endpoint used — a write must succeed on a fresh borrow.
        with app.state.db_pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO events (ts, agent_id, level, event_name, attributes, "
                "machine, process, category, source) "
                "VALUES (now(), %s, 'info', 'task_update', '{}'::jsonb, "
                "'test', 'test', 'audit', 'test')",
                (aid,),
            )


# ── LogQL inspector metrics (task #1280) ──────────────────────────────────────


def _logql_metric(
    name: str = "agent_llm_cost",
    *,
    panel: str = "timeseries",
    query: str | None = None,
    output: list[str] | None = None,
) -> dict[str, Any]:
    """A registry-row dict for a logql inspector metric."""
    return {
        "name": name,
        "title": "Agent LLM cost",
        "description": "logql inspector metric",
        "event_name": "llm_usage",
        "category": "telemetry",
        "unit": "currencyUSD",
        "panel": panel,
        "query": query
        or (
            'sum(sum_over_time({service_name="unknown_service"} | json | '
            "category={category} | event_name={event_name} | {{agent_id}} | "
            "unwrap attributes_cost_usd [$__interval]))"
        ),
        "output": output or ["inspector"],
        "plugin": "test_plugin",
        "query_type": "logql",
    }


class _FakeLokiResponse:
    """httpx.Response stand-in carrying a Loki query_range payload."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


def _client_factory(client: object) -> Any:
    """Named (typed) stand-in for httpx.Client so monkeypatched lambdas
    don't trip pyright's partially-unknown-lambda rule."""

    def _make(*args: object, **kwargs: object) -> Any:
        return client

    return _make


class _FakeLokiClient:
    """Records the query it was asked to run and returns a fixed series."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.last_params: dict[str, Any] | None = None
        self.last_url: str | None = None

    def __enter__(self) -> _FakeLokiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, url: str, params: dict[str, Any]) -> _FakeLokiResponse:
        self.last_url = url
        self.last_params = params
        return _FakeLokiResponse(self.payload)


def _loki_payload(series: list[tuple[str, str]]) -> dict[str, Any]:
    """One-result query_range payload (values as (ts_ns str, value str))."""
    return {"status": "success", "data": {"result": [{"metric": {}, "values": series}]}}


def test_metrics_logql_timeseries_via_loki(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A logql inspector metric executes against Loki (query_range over the
    fixed 24h window, 1h steps) instead of Postgres, and the series folds
    into the same PluginMetricResult shape."""

    from gateway import loki_events

    aid = _insert_agent(db_conn)
    _write_registry(_logql_metric())
    db_conn.commit()
    fake = _FakeLokiClient(
        _loki_payload(
            [
                ("1786726800000000000", "0.1"),
                ("1786730400000000000", "0.2"),
                ("1786734000000000000", "0.3"),
            ]
        )
    )
    monkeypatch.setattr(loki_events.httpx, "Client", _client_factory(fake))
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    m = body[0]
    assert m["error"] is None and m["value"] is None
    assert len(m["series"]) == 3
    # query_range params: the fixed window with 1h steps
    assert fake.last_params is not None
    assert fake.last_params["step"] == "3600"
    assert fake.last_url is not None and fake.last_url.endswith("/loki/api/v1/query_range")
    # the rendered query carried the agent filter and the translated window
    sent = fake.last_params["query"]
    assert f'agent_id="{aid}"' in sent
    assert "[1h]" in sent
    assert "$__" not in sent


def test_metrics_logql_stat_via_loki(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stat-shaped logql metric returns the last bucket as `value`."""
    from gateway import loki_events

    aid = _insert_agent(db_conn)
    _write_registry(
        _logql_metric(
            name="stat_cost",
            panel="stat",
            query=(
                'sum(sum_over_time({service_name="unknown_service"} | json | '
                "category={category} | event_name={event_name} | {{agent_id}} | "
                "unwrap attributes_cost_usd [$__range]))"
            ),
        )
    )
    db_conn.commit()
    fake = _FakeLokiClient(
        _loki_payload([("1786726800000000000", "0.1"), ("1786730400000000000", "0.2")])
    )
    monkeypatch.setattr(loki_events.httpx, "Client", _client_factory(fake))
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 200
    m = resp.json()[0]
    assert m["panel"] == "stat"
    assert m["value"] == 0.2
    assert m["series"] == []
    # $__range translated to the 24h instant window
    assert "[24h]" in fake.last_params["query"]  # type: ignore[index]


def test_metrics_logql_loki_failure_is_per_metric(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Loki error lands in the metric's `error` field; sibling SQL metrics
    still render."""
    from gateway import loki_events

    aid = _insert_agent(db_conn)
    _write_registry(
        _logql_metric(),
        _metric(name="still_fine"),
    )
    db_conn.commit()

    class _BrokenClient:
        def __enter__(self) -> _BrokenClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, params: dict[str, Any]) -> _FakeLokiResponse:
            import httpx

            raise httpx.ConnectError("loki down", request=httpx.Request("GET", "http://loki"))

    monkeypatch.setattr(loki_events.httpx, "Client", _client_factory(_BrokenClient()))
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 200
    body = resp.json()
    names = {m["name"]: m for m in body}
    assert "query failed" in names["agent_llm_cost"]["error"]
    assert names["still_fine"]["error"] is None


def test_metrics_logql_tampered_query_500(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tampered logql registry row (lost selector / json pipeline) fails
    the rendered-form re-validation -> 500, never executed."""
    aid = _insert_agent(db_conn)
    _write_registry(
        _logql_metric(
            name="tampered_logql",
            query='sum(count_over_time({other="x"} | json | event_name={event_name} [1h]))',
        )
    )
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 500
    assert "re-validation" in resp.json()["detail"]


def test_metrics_logql_macro_translation_unit() -> None:
    """LogQL macros translate to the fixed Loki window: $__interval -> 1h
    (Go duration syntax — '1 hour' is not parseable), $__range -> 24h."""
    translated = _translate_macros(
        'sum(count_over_time({service_name="unknown_service"} | json | '
        'event_name="x" [$__interval])) + sum(sum_over_time({service_name="unknown_service"} | json | '
        'event_name="x" | unwrap attributes_cost_usd [$__range]))',
        logql=True,
    )
    assert "[1h]" in translated
    assert "[24h]" in translated
    assert "$__" not in translated
