"""Integration tests for GET /api/agents/{id}/inspect/metrics (W13b).

The inspector surface of the plugin metric system: the gateway builds the
metric registry in process (task #180 PR D — mocked here by patching
`_load_plugin_metrics`), keeps `output`-inspector metrics, renders each
template for the agent, re-validates the rendered query, substitutes the
Grafana time macros with a fixed window, and executes the query — LogQL
against a fake Loki client, SQL against the test DB.

Locks: empty registry -> [], unknown agent -> 404, tampered template -> 500
with the reason, {{agent_id}} template rendered with no agent id -> 400,
execution-time query failure -> per-metric `error` while the other metrics
still render, and the execution semantics (event_name/category literals,
per-agent filtering, stat vs series payloads, macro translation).
"""

from __future__ import annotations

import importlib
import sys
from contextlib import contextmanager
from typing import Any, Literal

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.routers import _plugin_metrics
from gateway.routers._plugin_metrics import (
    _render_metric_query,
    _translate_macros,
)
from shared.plugin_metrics import MetricSpec

# A valid inspector query — the static-SQL shape (task #180 PR C): the
# template era (macros + {event_name}/{category}/{{agent_id}} placeholders)
# is over; the per-agent inspector idiom lives in the LogQL dialect. A
# timeseries SQL metric is no longer expressible (bucketing was a macro
# feature), so the SQL inspector surface is stat-shaped (core_live_agents).
# Post-#1823 the SQL metric dialect reads live tables only (the frozen
# `events` archive was dropped); `agents_meta` is the surviving live shape
# (see core_live_agents). The demo/stat queries below are agents_meta-based.
_DEMO_QUERY = (
    "SELECT 100.0 * count(*) FILTER (WHERE status = 'running') "
    '/ NULLIF(count(*), 0) AS "running %" '
    "FROM agents_meta"
)

# A stat-shaped inspector query (one aggregate row).
_STAT_QUERY = (
    "SELECT count(*) AS live "
    "FROM agents_meta "
    "WHERE status IN ('running', 'idling')"
)


def _metric(
    name: str = "demo_task_done_rate",
    *,
    event_name: str = "task_update",
    category: str = "audit",
    panel: str = "stat",
    query: str = _DEMO_QUERY,
    output: list[str] | None = None,
    unit: str = "percent",
) -> dict[str, Any]:
    """A registry-row dict (the shape a MetricSpec registration carries)."""
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


def _patch_loader(monkeypatch: pytest.MonkeyPatch, *metrics: dict[str, Any]) -> None:
    """Seed the in-process loader with the given registry rows — the
    task #180 PR D equivalent of the old snapshot file (the loader itself is
    covered separately by `test_in_process_loader_imports_shipped_metrics`)."""
    specs = [MetricSpec.model_validate(m) for m in metrics]
    monkeypatch.setattr(_plugin_metrics, "_load_plugin_metrics", lambda: specs)


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


# ── file absent / agent unknown ───────────────────────────────────────────────


def test_metrics_empty_registry_returns_empty(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No registered metrics -> 200 []."""
    _patch_loader(monkeypatch)
    aid = _insert_agent(db_conn)
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 200
    assert resp.json() == []


def test_metrics_unknown_agent_404(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No agents_meta row -> 404 (same contract as /inspect)."""
    _patch_loader(monkeypatch, _metric())
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get("/api/agents/999999/inspect/metrics")
    assert resp.status_code == 404


# ── filtering + rendering + execution ─────────────────────────────────────────


def test_metrics_filters_inspector_output(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only metrics whose output includes 'inspector' are returned; a
    grafana-only metric (even one sharing the same table shape) is skipped."""
    aid = _insert_agent(db_conn)
    _patch_loader(
        monkeypatch,
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


def test_metrics_stat_scalar(db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stat metric returns the single aggregate as `value`. The SQL
    inspector surface is static now (task #180 PR C — no macro windows), so
    the query counts exactly what its own predicates select."""
    aid = _insert_agent(db_conn)
    _insert_agent(db_conn)
    _patch_loader(monkeypatch, _metric(name="stat_count", panel="stat", query=_STAT_QUERY))
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["name"] == "stat_count"
    assert body[0]["panel"] == "stat"
    assert body[0]["value"] == 2
    assert body[0]["series"] == []
    assert body[0]["error"] is None


def test_metrics_macro_translation_unit() -> None:
    """The Grafana macros translate to the fixed inspector window; macro
    arguments (file-controlled) are consumed wholesale and never reach the
    output."""
    sql = _translate_macros(
        "SELECT $__timeGroup(ts, $__interval) AS time, count(*) "
        "FROM events WHERE event_name = 'k' AND $__timeFilter(ts) "
        "AND $__timeFilter(ts, 'extra')"
    )
    # the double AT TIME ZONE round-trip truncates in UTC while keeping the
    # bucket column a tz-aware timestamptz (see _MACRO_TIMEGROUP)
    assert "date_trunc('hour', ts AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'" in sql
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
def test_metrics_tampered_query_500(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tampered: str
) -> None:
    """A registry file edited after generation fails the second validation ->
    500 with the reason (never executed)."""
    aid = _insert_agent(db_conn)
    _patch_loader(
        monkeypatch,
        _metric(name="tampered", query=tampered),
        _metric(name="still_fine"),
    )
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 500
    assert "re-validation" in resp.json()["detail"]


def test_metrics_render_requires_agent_id_400() -> None:
    """A {{agent_id}} template rendered without an agent id is refused with
    400. (The HTTP route always carries the id in the path, so this is a
    helper-level guard — defense in depth against a future caller.) The
    {{agent_id}} idiom lives in the LogQL dialect (task #180 PR C)."""
    spec = MetricSpec.model_validate(_logql_metric())
    with pytest.raises(HTTPException) as exc_info:
        _render_metric_query(spec, agent_id=None)
    assert exc_info.value.status_code == 400


def test_metrics_render_static_sql_passes_through() -> None:
    """A static SQL query renders verbatim and passes the whitelist (the
    template era is over, task #180 PR C)."""
    spec = MetricSpec.model_validate(_metric())
    query = _render_metric_query(spec, agent_id=7)
    assert query == _DEMO_QUERY
    assert "{event_name}" not in query


# ── execution-time failure is per-metric, not fatal ───────────────────────────


def test_metrics_runtime_query_error_per_metric(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A query that passes the whitelist but fails at runtime (a bad cast on
    the row values) lands in that metric's `error`; the sibling metric still
    renders and the request stays 200."""
    aid = _insert_agent(db_conn)
    _patch_loader(
        monkeypatch,
        _metric(
            name="broken",
            query=(
                "SELECT status::bigint AS n FROM agents_meta "
                "WHERE id = %(agent_id)s"
            ),
        ),
        _metric(name="healthy", panel="stat", query=_STAT_QUERY),
    )
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 200
    by_name = {m["name"]: m for m in resp.json()}
    assert "bigint" in by_name["broken"]["error"].lower()
    assert by_name["healthy"]["error"] is None
    assert by_name["healthy"]["value"] == 1.0  # the seeded agents_meta row


def test_metrics_read_only_is_transaction_scoped(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read-only enforcement must NOT leak onto pooled connections.

    The endpoint opens its transaction with `SET TRANSACTION READ ONLY`
    (server-enforced, transaction-scoped) instead of `Connection.read_only` —
    that attribute is client-side state that persists on the pooled
    connection object after return, so a later borrower could inherit a
    read-only session and its writes would fail. After a metrics request,
    the gateway's own pool must still accept writes."""
    aid = _insert_agent(db_conn)
    _patch_loader(monkeypatch, _metric(name="stat_count", panel="stat", query=_STAT_QUERY))
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
        assert resp.status_code == 200
        assert resp.json()[0]["value"] == 1
        # Same pool the endpoint used — a write must succeed on a fresh borrow.
        with app.state.db_pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents (label) VALUES ('post-metrics-write')",
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


def _client_accessor(client: object) -> Any:
    """Named (typed) stand-in for `loki_events._client` so monkeypatched
    lambdas don't trip pyright's partially-unknown-lambda rule."""

    def _get() -> Any:
        return client

    return _get


class _FakeLokiClient:
    """Records the query it was asked to run and returns a fixed series."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.last_params: dict[str, Any] | None = None
        self.last_url: str | None = None

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
    _patch_loader(monkeypatch, _logql_metric())
    db_conn.commit()
    fake = _FakeLokiClient(
        _loki_payload(
            [
                ("1786726800", "0.1"),
                ("1786730400", "0.2"),
                ("1786734000", "0.3"),
            ]
        )
    )
    monkeypatch.setattr(loki_events, "_client", _client_accessor(fake))
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
    _patch_loader(
        monkeypatch,
        _logql_metric(
            name="stat_cost",
            panel="stat",
            query=(
                'sum(sum_over_time({service_name="unknown_service"} | json | '
                "category={category} | event_name={event_name} | {{agent_id}} | "
                "unwrap attributes_cost_usd [$__range]))"
            ),
        ),
    )
    db_conn.commit()
    fake = _FakeLokiClient(_loki_payload([("1786726800", "0.1"), ("1786730400", "0.2")]))
    monkeypatch.setattr(loki_events, "_client", _client_accessor(fake))
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
    _patch_loader(
        monkeypatch,
        _logql_metric(),
        _metric(name="still_fine"),
    )
    db_conn.commit()

    class _BrokenClient:
        def get(self, url: str, params: dict[str, Any]) -> _FakeLokiResponse:
            import httpx

            raise httpx.ConnectError("loki down", request=httpx.Request("GET", "http://loki"))

    monkeypatch.setattr(loki_events, "_client", _client_accessor(_BrokenClient()))
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert [m["name"] for m in body] == ["agent_llm_cost", "still_fine"]
    names = {m["name"]: m for m in body}
    assert "query failed" in names["agent_llm_cost"]["error"]
    assert names["still_fine"]["error"] is None


@pytest.mark.parametrize("reason", ["queue_full", "acquire_timeout"])
def test_metrics_logql_local_budget_rejection_is_503(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    reason: Literal["queue_full", "acquire_timeout"],
) -> None:
    """A local budget refusal is endpoint saturation, not a per-metric Loki error."""
    from gateway import loki_events, loki_query_budget

    aid = _insert_agent(db_conn)
    _patch_loader(monkeypatch, _logql_metric())
    db_conn.commit()

    def reject(*args: Any, **kwargs: Any) -> list[tuple[str, float]]:
        raise loki_query_budget.LokiQueryBudgetError(reason)

    monkeypatch.setattr(loki_events, "metric_range", reject)
    with TestClient(app) as client:
        response = client.get(f"/api/agents/{aid}/inspect/metrics")
    assert response.status_code == 503
    assert response.json()["detail"] == f"Loki query budget unavailable ({reason}); retry"
    assert response.headers["retry-after"] == "1"


def test_metrics_logql_releases_db_connection_before_waiting_for_loki(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued Loki query must not consume a scarce Postgres pool slot."""
    from gateway import loki_events

    class TrackingCursor:
        def execute(self, query: str, params: tuple[int] | None = None) -> None:
            if query == "SET TRANSACTION READ ONLY":
                assert params is None
                return
            assert query == "SELECT 1 FROM agents_meta WHERE id = %s"
            assert params == (7,)

        def fetchone(self) -> tuple[int]:
            return (1,)

    class TrackingConnection:
        @contextmanager
        def cursor(self) -> Any:
            yield TrackingCursor()

    class TrackingPool:
        active = False

        @contextmanager
        def connection(self) -> Any:
            self.active = True
            try:
                yield TrackingConnection()
            finally:
                self.active = False

    pool = TrackingPool()
    _patch_loader(monkeypatch, _logql_metric())

    def metric_range(*args: Any, **kwargs: Any) -> list[tuple[str, float]]:
        assert not pool.active
        return []

    monkeypatch.setattr(loki_events, "metric_range", metric_range)
    result = _plugin_metrics.metrics_for_agent(pool, 7)  # type: ignore[arg-type]
    assert len(result) == 1
    assert result[0].error is None


def test_metrics_logql_tampered_query_500(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tampered logql registry row (lost selector / json pipeline) fails
    the rendered-form re-validation -> 500, never executed."""
    aid = _insert_agent(db_conn)
    _patch_loader(
        monkeypatch,
        _logql_metric(
            name="tampered_logql",
            query='sum(count_over_time({other="x"} | json | event_name={event_name} [1h]))',
        ),
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


# ── in-process registry (task #180 PR D) ──────────────────────────────────────


def test_in_process_loader_imports_shipped_metrics() -> None:
    """The loader imports every shipped plugin metrics.py under its plugin
    context plus the core definition modules — plugin metrics first, then
    core, the old snapshot's two-section order. No file involved."""
    from shared import core_metrics
    from shared.plugin_context import PluginContext
    from shared.plugin_metrics import clear_registry

    # Re-run the registrations fresh — earlier tests in the session may have
    # cleared or reloaded the process-global registries (a module already in
    # sys.modules must be reloaded, a fresh one only imported once).

    clear_registry()
    core_metrics.clear_core_registry()
    for name in ("ava_code", "ava_fleet", "ava_memory"):
        mod_name = f"ava_builtins.plugins.{name}.metrics"
        mod = sys.modules.get(mod_name)
        with PluginContext(name):
            if mod is None:
                importlib.import_module(mod_name)
            else:
                importlib.reload(mod)
    for mod_name in ("shared.core_metrics_panels", "shared.core_metrics_observability"):
        mod = sys.modules.get(mod_name)
        if mod is None:
            importlib.import_module(mod_name)
        else:
            importlib.reload(mod)

    specs = _plugin_metrics._load_plugin_metrics()

    plugin_specs = [s for s in specs if s.plugin != "core"]
    core_specs = [s for s in specs if s.plugin == "core"]
    # the shipped plugin metrics (9 after retiring the duplicate spawn panel)
    assert {s.plugin for s in plugin_specs} == {"ava_code", "ava_fleet", "ava_memory"}
    assert len(plugin_specs) == 9
    # core section follows, plugin section first (old snapshot order)
    assert [s.plugin for s in specs].index("core") == len(plugin_specs)
    assert len(core_specs) >= 16
    # a second call is the same objects (module cache, no re-registration)
    assert _plugin_metrics._load_plugin_metrics() == specs
