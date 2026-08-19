"""Inspector plugin-metric execution — helper for agent_inspect (W13b).

Not a router: ``gateway/routers/agent_inspect.py`` mounts the single endpoint
``GET /api/agents/{id}/inspect/metrics`` and delegates the blocking work here
(kept as its own module so agent_inspect stays under the per-file line budget).
The inspector surface of the plugin metric system (W13, PR #1374): read the
registry snapshot the generator exports ($AVA_HOME/state/plugin_metrics.json,
written by scripts/gen_plugin_dashboard.py), keep the metrics whose `output`
includes "inspector", render each template for the requested agent,
re-validate the rendered SQL (the persisted file is disk input — register-time
validation is not enough), substitute the Grafana time macros with a fixed
recent window (the templates are written for Grafana's query-time injection;
the inspector has no dashboard time range), and execute the query read-only
against the cluster's own Postgres. One `PluginMetricResult` per metric; a
metric whose query fails at execution time carries an `error` field instead of
failing the whole request, while registry-level problems (missing file -> [],
unreadable/invalid file -> 500, template failing the safety re-validation ->
500, `{{agent_id}}` template without an agent id -> 400) are HTTP errors.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString, cast

import httpx
import psycopg
from fastapi import HTTPException
from psycopg import Connection, Cursor
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from gateway import loki_events
from gateway.schemas import MetricPoint, PluginMetricResult
from shared.metrics_logql import validate_logql
from shared.plugin_metrics import (
    MetricSpec,
    PluginMetricError,
    render_query,
    validate_metric_sql,
)

# The registry snapshot path — matches the generator's `_default_state_out`
# (scripts/gen_plugin_dashboard.py). A function so tests can point it at a
# fixture file.
_PLUGIN_METRICS_STATE_FILE = "plugin_metrics.json"
_PLUGIN_METRICS_SCHEMA_VERSION = 1

# The inspector's fixed recent window: the templates' `$__timeFilter(ts)` /
# `$__timeGroup(ts, $__interval)` render to this window (24h in 1h buckets =
# at most 24 series points per metric). Deliberately NOT a request parameter:
# the panel is a compact at-a-glance surface, and a fixed window keeps the
# per-poll DB cost constant.
_INSPECTOR_WINDOW_HOURS = 24
_INSPECTOR_BUCKET = "1 hour"
_INSPECTOR_BUCKET_UNIT = "hour"  # date_trunc field name (no count)

# Row cap per metric: the whitelist cannot forbid a template without LIMIT, so
# a tampered-but-valid query could otherwise return the whole table. Fetch at
# most this many rows per metric (a legit windowed series never gets close).
_MAX_METRIC_ROWS = 500

# The macro -> SQL translation table. The replacement strings are module
# constants (trusted gateway code); the macro *arguments* from the registry
# file are consumed by the regex and never reach the DB — a tampered file can
# put anything whitelisted inside the parens, it is discarded wholesale.
# The double `AT TIME ZONE 'UTC'` round-trip (timestamptz -> naive UTC wall
# clock -> back to timestamptz) truncates the bucket in UTC while keeping the
# result a tz-aware timestamptz: `MetricPoint.ts` (gateway/schemas/inspect.py)
# is fed this column directly, and a naive result would silently serialize
# without a UTC offset.
_MACRO_TIMEGROUP = (
    f"date_trunc('{_INSPECTOR_BUCKET_UNIT}', ts AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
)
_MACRO_TIMEFILTER = f"ts >= now() - interval '{_INSPECTOR_WINDOW_HOURS} hours'"
_MACRO_INTERVAL = f"interval '{_INSPECTOR_BUCKET}'"
_MACRO_INTERVAL_MS = str(3600 * 1000)  # 1 hour in ms
# Loki range-vector window for the same bucket — Go duration syntax ("1 hour"
# is not parseable by Loki's duration parser).
_MACRO_INTERVAL_LOKI = "1h"
_MACRO_RANGE_LOKI = f"{_INSPECTOR_WINDOW_HOURS}h"


def _plugin_metrics_state_path() -> Path:
    """$AVA_HOME/state/plugin_metrics.json — the registry snapshot written by
    ``scripts/gen_plugin_dashboard.py`` (W13). The gateway only ever reads it;
    the generator is the single writer."""
    from shared import paths

    return paths.ava_home() / "state" / _PLUGIN_METRICS_STATE_FILE


def _load_plugin_metrics() -> list[MetricSpec]:
    """Read + parse the registry snapshot.

    Missing file -> [] (the generator has not run / no plugin registers
    metrics — the inspector panel renders nothing). Any other problem (unreadable
    file, non-JSON, wrong schema_version, a row that does not parse as a
    MetricSpec) is a 500 with the concrete reason — the file is generated from
    plugin code, so an invalid file means a bug/tampering that should be loud,
    not silently skipped."""
    path = _plugin_metrics_state_path()
    if not path.exists():
        return []
    try:
        raw: dict[str, Any] = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"plugin metrics registry {path} is unreadable: {exc}",
        ) from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != _PLUGIN_METRICS_SCHEMA_VERSION:
        raise HTTPException(
            status_code=500,
            detail=(
                f"plugin metrics registry {path} has an unsupported schema_version "
                f"(expected {_PLUGIN_METRICS_SCHEMA_VERSION})"
            ),
        )
    try:
        # Core metrics (Task #882) live in the same snapshot under a
        # `core_metrics` section; both surfaces render identically.
        rows = list(raw.get("metrics", [])) + list(raw.get("core_metrics", []))
        return [MetricSpec.model_validate(m) for m in rows]
    except (KeyError, ValidationError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"plugin metrics registry {path} contains an invalid metric spec: {exc}",
        ) from exc


def _render_metric_query(spec: MetricSpec, agent_id: int | None) -> str:
    """Render one template for the inspector surface and re-validate the result.

    Rendering substitutes {event_name}/{category} as single-quoted literals and
    {{agent_id}} as ``agent_id = <n>``. The SECOND validation runs on the
    rendered SQL — the persisted registry is disk input, and the register-time
    check (W13) does not protect against a tampered file; this one refuses
    anything that no longer parses as a whitelisted single SELECT.

    Raises:
        HTTPException 400: the template uses {{agent_id}} but `agent_id` is
            None — the metric is per-agent and cannot run unparameterized.
        HTTPException 500: the rendered SQL failed `validate_metric_sql` —
            the file diverged from what register_metric allowed.
    """
    query = render_query(spec, agent_id=agent_id)
    if "{{agent_id}}" in query:
        raise HTTPException(
            status_code=400,
            detail=(
                f"metric {spec.name!r} requires the {{{{agent_id}}}} placeholder — "
                "call GET /api/agents/{id}/inspect/metrics with the agent id"
            ),
        )
    try:
        if spec.query_type == "logql":
            validate_logql(query, spec.name)
        else:
            validate_metric_sql(query)
    except PluginMetricError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"metric {spec.name!r} failed the inspector safety re-validation "
                f"(registry {_plugin_metrics_state_path()} diverges from what "
                f"register_metric allowed): {exc}"
            ),
        ) from exc
    return query


def _translate_macros(sql: str, *, logql: bool = False) -> str:
    """Substitute the Grafana time macros with the inspector's fixed window.

    The registry templates are written for Grafana, which injects
    ``$__timeFilter(ts)`` / ``$__timeGroup(ts, $__interval)`` at query time;
    the inspector surface has no dashboard time range, so the gateway
    substitutes its own constants instead. SQL templates get the date_trunc /
    now() forms; LogQL templates (task #1280) get the fixed range-vector
    window / instant range. The output is intentionally NOT re-validated:
    it contains ``now()`` / ``interval`` / ``date_trunc`` (SQL) or the fixed
    window strings (LogQL) which are outside the template whitelist by design
    — the substitution is trusted gateway code (module constants), while
    everything the file controls was validated before this ran."""
    if logql:
        return sql.replace("$__interval", _MACRO_INTERVAL_LOKI).replace(
            "$__range", _MACRO_RANGE_LOKI
        )
    sql = re.sub(r"\$__timeGroup\s*\([^)]*\)", _MACRO_TIMEGROUP, sql)
    sql = re.sub(r"\$__timeFilter\s*\([^)]*\)", _MACRO_TIMEFILTER, sql)
    return sql.replace("$__interval_ms", _MACRO_INTERVAL_MS).replace("$__interval", _MACRO_INTERVAL)


def _as_float(value: Any) -> float | None:
    """DB value -> float for the JSON response; None when it is NULL or
    non-numeric (the metric's query decided to emit something else — the panel
    renders it as missing rather than crash)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _execute_metric(
    conn: Connection[Any], cur: Cursor, spec: MetricSpec, query: str
) -> PluginMetricResult:
    """Run one rendered query and fold the rows into a PluginMetricResult.

    `panel` picks the payload shape: stat -> `value` (first row, first column);
    timeseries / barchart -> `series` (rows as (bucket ts, value), capped at
    _MAX_METRIC_ROWS). The query runs inside a savepoint (`conn.transaction()`
    nests as one when the outer transaction is open), so a failing query rolls
    back only its own savepoint — a whitelist-valid template that fails at
    runtime (e.g. a missing column) lands in the result's `error` field
    without poisoning the sibling metrics' queries in the same transaction."""
    base = PluginMetricResult(
        name=spec.name,
        title=spec.title,
        description=spec.description,
        plugin=spec.plugin,
        unit=spec.unit,
        panel=spec.panel,
    )
    if spec.query_type == "logql":
        return _execute_metric_logql(spec, query)
    try:
        with conn.transaction():
            # The query is the rendered + re-validated template (see
            # _render_metric_query) — a str that psycopg's typing only accepts
            # as LiteralString; the cast documents that the injection safety
            # was established by validate_metric_sql, not by psycopg.
            cur.execute(cast(LiteralString, query))
            rows = cur.fetchmany(_MAX_METRIC_ROWS + 1)
    except psycopg.Error as exc:
        return base.model_copy(update={"error": f"query failed: {exc}"})
    if spec.panel == "stat":
        value = _as_float(rows[0][0]) if rows else None
        return base.model_copy(update={"value": value})
    # timeseries / barchart contract: (bucket ts, value) rows. A template
    # returning a different shape is a plugin bug — report it per metric
    # instead of crashing the whole panel.
    if any(len(row) < 2 for row in rows):
        return base.model_copy(update={"error": "timeseries query must return (ts, value) columns"})
    series = [
        MetricPoint(ts=row[0], value=v)
        for row in rows[:_MAX_METRIC_ROWS]
        if (v := _as_float(row[1])) is not None
    ]
    return base.model_copy(update={"series": series})


def _execute_metric_logql(spec: MetricSpec, query: str) -> PluginMetricResult:
    """Run one rendered LogQL query against Loki and fold the series into a
    PluginMetricResult (task #1280).

    The query's $__interval/$__range macros were already translated to the
    inspector's fixed window, so the window here is now - 24h .. now with 1h
    steps (the range-vector windows align with the steps, giving
    non-overlapping hourly buckets). Loki failures (transport, status,
    unparseable payload) land in the result's `error` field like a SQL
    execution failure — the sibling metrics still render.
    """
    base = PluginMetricResult(
        name=spec.name,
        title=spec.title,
        description=spec.description,
        plugin=spec.plugin,
        unit=spec.unit,
        panel=spec.panel,
    )
    try:
        points = loki_events.metric_range(
            query,
            from_=datetime.now(UTC) - timedelta(hours=_INSPECTOR_WINDOW_HOURS),
            to=datetime.now(UTC),
            step_s=3600,
        )
    except (httpx.HTTPError, ValueError) as exc:
        return base.model_copy(update={"error": f"query failed: {exc}"})
    if spec.panel == "stat":
        return base.model_copy(update={"value": points[-1][1] if points else None})
    series = [MetricPoint(ts=datetime.fromisoformat(ts), value=v) for ts, v in points]
    return base.model_copy(update={"series": series})


def metrics_for_agent(pool: ConnectionPool[Any], agent_id: int) -> list[PluginMetricResult]:
    """Sync twin of the metrics endpoint — runs via asyncio.to_thread. One
    read-only transaction for the agent check + every metric query.

    Read-only is enforced server-side per transaction (`SET TRANSACTION READ
    ONLY` as the FIRST statement — a requirement of the command), NOT via
    `Connection.read_only`: that attribute is client-side state that persists
    on the pooled connection object after return, so a later borrower could
    inherit a read-only session and its writes would fail. The SET TRANSACTION
    form leaves nothing behind when the transaction rolls back."""
    specs = [s for s in _load_plugin_metrics() if "inspector" in s.output]
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SELECT 1 FROM agents_meta WHERE id = %s", (agent_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        return [
            _execute_metric(
                conn,
                cur,
                spec,
                _translate_macros(
                    _render_metric_query(spec, agent_id), logql=spec.query_type == "logql"
                ),
            )
            for spec in specs
        ]
