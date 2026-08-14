"""SQL safety validation + registration for plugin metrics (W13).

Covers the three things ``register_metric`` enforces at import time: name
uniqueness, template SQL safety (single SELECT over `events` only, function /
macro / operator whitelist), and the ``{{agent_id}}`` ↔ output-surface rule.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from shared.plugin_context import PluginContext
from shared.plugin_metrics import (
    DuplicateMetric,
    InvalidMetricQuery,
    MetricSpec,
    NoPluginContext,
    clear_registry,
    export_registry,
    register_metric,
    render_query,
    render_targets,
    validate_metric_sql,
)

# ── SQL safety: accepted templates ────────────────────────────────────────────


def _good_sqls() -> list[str]:
    return [
        # plain windowed count (the ava-ops-main.json style)
        "SELECT count(*) AS calls FROM events WHERE event_name='llm_usage' "
        "AND category IN ('telemetry','log') AND $__timeFilter(ts)",
        # placeholders + $__timeGroup + quoted alias
        'SELECT $__timeGroup(ts, $__interval) AS time, count(*) AS "calls" '
        "FROM events WHERE event_name = {event_name} AND category = {category} "
        "AND $__timeFilter(ts) GROUP BY 1 ORDER BY 1",
        # FILTER + JSONB access + NULLIF division (the fleet done-rate shape)
        "SELECT 100.0 * count(*) FILTER (WHERE attributes->>'status' = 'done') "
        '/ NULLIF(count(*), 0) AS "done %" FROM events '
        "WHERE event_name = {event_name} AND category = {category} AND attributes ? 'status' "
        "AND $__timeFilter(ts)",
        # subquery with its own FROM events (W12 per-agent shape)
        'SELECT bucket AS time, max(ratio) AS "max agent" FROM ('
        "SELECT $__timeGroup(ts, $__interval) AS bucket, agent_id, "
        "SUM((attributes->>'cache_read')::bigint)::numeric "
        "/ NULLIF(SUM((attributes->>'in_total')::bigint), 0) AS ratio "
        "FROM events WHERE event_name = 'llm_usage' AND category = 'telemetry' "
        "AND agent_id IS NOT NULL AND $__timeFilter(ts) GROUP BY 1, 2) per_agent "
        "GROUP BY 1 ORDER BY 1",
        # trailing semicolon tolerated
        "SELECT count(*) FROM events WHERE event_name = {event_name} "
        "AND category = {category} AND $__timeFilter(ts);",
        # comparisons / arithmetic / IS NULL / LIMIT / OFFSET / bare columns
        "SELECT count(*) FROM events WHERE agent_id IS NULL LIMIT 10 OFFSET 5",
        # CASE expression + comparisons
        "SELECT count(*) FILTER (WHERE CASE WHEN event_name = 'a' THEN 1 ELSE 0 END = 1) FROM events",
        # ordered-set aggregate (W18): percentile_cont + WITHIN GROUP
        "SELECT percentile_cont(0.5) WITHIN GROUP "
        "(ORDER BY (attributes->>'duration_seconds')::float8) AS p50 FROM events",
        # keyword case-insensitivity (Tracker #766): the first keyword and the
        # table reference are accepted in any case, like every other keyword
        # check in the module
        "select count(*) from events where event_name = 'llm_usage' "
        "and category in ('telemetry', 'log') and $__timeFilter(ts)",
        "Select Count(*) From Events Where Kind = 'llm_usage'",
    ]


@pytest.mark.parametrize("sql", _good_sqls())
def test_validate_accepts_whitelisted_templates(sql: str) -> None:
    validate_metric_sql(sql)


def _bad_sqls() -> list[tuple[str, str]]:
    """(sql, expected fragment) — every template must be rejected and the
    message must say why."""
    return [
        ("INSERT INTO events (event_name) VALUES ('x')", "single SELECT"),
        ("UPDATE events SET event_name='x'", "single SELECT"),
        ("DELETE FROM events", "single SELECT"),
        ("SELECT 1; SELECT 2", "multi-statement"),
        ("SELECT count(*) FROM events; DROP TABLE events", "multi-statement"),
        ("SELECT count(*) FROM agent_events", "only the `events` table"),
        ("SELECT count(*) FROM events, agent_events", "only the `events` table"),
        ("SELECT count(*) FROM (SELECT 1 FROM agent_events) x", "only the `events`"),
        ("SELECT count(*) FROM information_schema.tables", "only the `events`"),
        ("SELECT version()", "not on the whitelist"),
        ("SELECT pg_sleep(1)", "not on the whitelist"),
        ("SELECT count(*) FROM events WHERE ts > now()", "not on the whitelist"),
        ("SELECT max(percentile_disc(0.5)) FROM events", "not on the whitelist"),
        ("SELECT string_agg(event_name, ',') FROM events", "not on the whitelist"),
        ("SELECT count(*) FROM events -- comment", "comments are not allowed"),
        ("SELECT count(*) FROM events /* c */", "comments are not allowed"),
        ("SELECT current_user", "not allowed"),
        ("SELECT current_date", "not allowed"),
        ("SELECT count(*) FROM events WHERE event_name = 'x' UNION SELECT 1", "not allowed"),
        ("WITH x AS (SELECT 1 FROM events) SELECT count(*) FROM x", "single SELECT"),
        ("SELECT count(*) FROM events HAVING count(*) > 1", "not allowed"),
        ("SELECT count(*) FROM events e WHERE event_name = 'x'", "alias"),
        ("SELECT $$dollar$$", "unrecognized"),
        ("SELECT count(*) FROM events WHERE event_name = {event_named}", "unrecognized"),
        ("EXPLAIN SELECT count(*) FROM events", "single SELECT"),
        ("SELECT count(*) FROM events WHERE event_name = 'x' OR 1=1 -- x", "comments"),
    ]


@pytest.mark.parametrize(("sql", "fragment"), _bad_sqls())
def test_validate_rejects_malicious_templates(sql: str, fragment: str) -> None:
    with pytest.raises(InvalidMetricQuery, match=fragment):
        validate_metric_sql(sql)


# ── Task #882 core-migration constructs ─────────────────────────────────────


def test_validate_accepts_core_migration_sql() -> None:
    """The SQL constructs the migrated core panels need: generate_series in
    FROM with alias column list, LEFT JOIN of a subquery with ON, agents_meta
    table, and the set/date helper functions."""
    validate_metric_sql(
        "SELECT g.time AS time, coalesce(d.n, 0) AS n "
        "FROM generate_series("
        "to_timestamp(floor(extract(epoch FROM $__timeFrom()::timestamptz) "
        "/ ($__interval_ms::float / 1000.0))), "
        "$__timeTo()::timestamptz, "
        "make_interval(secs => $__interval_ms::float / 1000.0)) AS g(time) "
        "LEFT JOIN ("
        "SELECT to_timestamp($__timeGroup(ts, $__interval)) AS time, "
        "count(*) AS n FROM events WHERE event_name = {event_name} "
        "AND category = {category} AND $__timeFilter(ts) GROUP BY 1"
        ") d ON d.time = g.time "
        "ORDER BY 1"
    )
    validate_metric_sql(
        "SELECT count(*) AS \"live agents\" FROM agents_meta WHERE status IN ('running','idling')"
    )
    validate_metric_sql(
        "SELECT $__timeGroup(ts, $__interval) AS time, "
        "sum((attributes->>'in_total')::numeric) "
        "/ NULLIF(sum((attributes->>'latency_ms')::numeric), 0) * 1000.0 AS tps "
        "FROM events WHERE event_name = {event_name} AND category = {category} "
        "AND attributes ? 'latency_ms' AND $__timeFilter(ts) "
        "GROUP BY 1 ORDER BY 1"
    )
    # multi-series: subquery per agent + outer max/min
    validate_metric_sql(
        'SELECT bucket AS time, max(tps) AS "max agent" FROM ('
        "SELECT $__timeGroup(ts, $__interval) AS bucket, agent_id, "
        "sum((attributes->>'in_total')::numeric) AS tps "
        "FROM events WHERE event_name = {event_name} GROUP BY 1, 2) q "
        "GROUP BY 1 ORDER BY 1"
    )
    # comma-separated tables still work
    validate_metric_sql(
        "SELECT count(*) FROM events, agents_meta WHERE events.agent_id = agents_meta.id"
    )


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        (
            "SELECT count(*) FROM generate_series(1, 10) LEFT JOIN events "
            "ON events.id = generate_series.generate_series",
            "LEFT JOIN must reference a subquery",
        ),
        (
            "SELECT count(*) FROM events LEFT JOIN agents_meta ON events.id = agents_meta.id",
            "LEFT JOIN must reference a subquery",
        ),
        # a subquery smuggled into generate_series args is still validated
        (
            "SELECT count(*) FROM generate_series((SELECT 1 FROM pg_class))",
            "only the `events`",
        ),
        # scalar subquery inside a SELECT-list function call is legal SQL
        # and must still be checked (2026-08-10 audit: coalesce args were
        # skipped wholesale, smuggling pg_class past the events-only gate)
        (
            "SELECT coalesce((SELECT count(*) FROM pg_class), 0) FROM events",
            "only the `events`",
        ),
        (
            "SELECT NULLIF((SELECT max(oid) FROM pg_class), 0) FROM events",
            "only the `events`",
        ),
    ],
)
def test_validate_rejects_bad_joins(sql: str, fragment: str) -> None:
    with pytest.raises(InvalidMetricQuery, match=fragment):
        validate_metric_sql(sql)


def test_validate_accepts_scalar_subquery_over_events() -> None:
    """A scalar subquery inside a whitelisted function call is legal SQL
    when it reads from `events` — the FROM gate recurses into call args
    but must not reject the legitimate case (2026-08-10 audit fix)."""
    validate_metric_sql(
        "SELECT coalesce((SELECT count(*) FROM events), 0) FROM events "
        "WHERE event_name = {event_name} AND $__timeFilter(ts)"
    )
    validate_metric_sql("SELECT extract(epoch FROM ts) AS t FROM events WHERE $__timeFilter(ts)")


def test_validate_rejects_unknown_from_function() -> None:
    with pytest.raises(InvalidMetricQuery, match="only the `events`"):
        validate_metric_sql("SELECT count(*) FROM unnest(ARRAY(1,2))")


# ── multi-target specs ────────────────────────────────────────────────────────


def test_render_targets_renders_query_and_targets() -> None:
    spec = MetricSpec(
        name="multi",
        title="Multi",
        event_name="turn_end",
        category="telemetry",
        query="SELECT count(*) FROM events WHERE event_name = {event_name}",
        targets=[
            "SELECT count(*) FROM events WHERE category = {category}",
            "SELECT count(*) FROM events WHERE agent_id = {{agent_id}}",
        ],
    )
    rendered = render_targets(spec)
    assert rendered == [
        "SELECT count(*) FROM events WHERE event_name = 'turn_end'",
        "SELECT count(*) FROM events WHERE category = 'telemetry'",
        "SELECT count(*) FROM events WHERE agent_id = {{agent_id}}",
    ]
    rendered_agent = render_targets(spec, agent_id=7)
    assert rendered_agent[2].endswith("agent_id = 7")


def test_register_validates_all_targets() -> None:
    from shared.plugin_context import PluginContext

    with pytest.raises(InvalidMetricQuery, match="not on the whitelist"), PluginContext("t"):
        register_metric(
            MetricSpec(
                name="bad_target",
                title="Bad",
                event_name="x",
                category="log",
                query="SELECT count(*) FROM events",
                targets=["SELECT pg_sleep(1)"],
            )
        )


# ── registration ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _spec(**overrides: Any) -> MetricSpec:
    base: dict[str, Any] = {
        "name": "test_metric",
        "title": "Test Metric",
        "event_name": "turn_end",
        "category": "telemetry",
        "query": "SELECT count(*) FROM events WHERE event_name = {event_name} "
        "AND category = {category} AND $__timeFilter(ts)",
    }
    base.update(overrides)
    return MetricSpec(**base)  # type: ignore[arg-type]


def test_register_fills_plugin_from_context() -> None:
    with PluginContext("ava_demo"):
        registered = register_metric(_spec())
    assert registered.plugin == "ava_demo"
    assert registered_metrics_names() == ["test_metric"]


def registered_metrics_names() -> list[str]:
    from shared.plugin_metrics import registered_metrics

    return [m.name for m in registered_metrics()]


def test_register_outside_context_raises() -> None:
    with pytest.raises(NoPluginContext):
        register_metric(_spec())


def test_register_duplicate_name_rejected() -> None:
    with PluginContext("ava_demo"):
        register_metric(_spec(name="dup"))
        with pytest.raises(DuplicateMetric):
            register_metric(_spec(name="dup"))


def test_register_bad_event_name_rejected() -> None:
    with pytest.raises(ValidationError):
        _spec(event_name="TurnEnd")  # uppercase — must be ^[a-z][a-z0-9_-]*$
    with pytest.raises(ValidationError):
        _spec(event_name="turn end")
    with pytest.raises(ValidationError):
        _spec(event_name="turn-end!")


def test_register_hyphenated_event_name_accepted() -> None:
    # The live event vocabulary carries hyphens (e.g. recall-filter), so the
    # event_name charset allows them; the renderer quotes event_name as a literal, so this
    # cannot broaden the SQL surface.
    with PluginContext("ava_demo"):
        ok = register_metric(_spec(event_name="recall-filter"))
    assert ok.event_name == "recall-filter"
    assert "event_name = 'recall-filter'" in render_query(ok)


def test_register_bad_category_rejected() -> None:
    with pytest.raises(ValidationError):
        _spec(category="metrics")  # not audit|telemetry|log


def test_register_agent_placeholder_requires_inspector_only() -> None:
    query = (
        "SELECT count(*) FROM events WHERE event_name = {event_name} AND category = {category} "
        "AND {{agent_id}} AND $__timeFilter(ts)"
    )
    with pytest.raises(InvalidMetricQuery, match="agent_id"), PluginContext("ava_demo"):
        register_metric(_spec(query=query, output=["grafana", "inspector"]))
    with PluginContext("ava_demo"):
        ok = register_metric(_spec(query=query, output=["inspector"]))
    assert ok.output == ["inspector"]


def test_register_duplicate_output_surface_rejected() -> None:
    with pytest.raises(ValidationError):
        _spec(output=["grafana", "grafana"])


def test_register_empty_output_rejected() -> None:
    with pytest.raises(ValidationError):
        _spec(output=[])


# ── rendering + export ────────────────────────────────────────────────────────


def test_render_substitutes_placeholders_as_literals() -> None:
    with PluginContext("ava_demo"):
        spec = register_metric(_spec())
    rendered = render_query(spec)
    assert "event_name = 'turn_end'" in rendered
    assert "category = 'telemetry'" in rendered
    assert "{event_name}" not in rendered and "{category}" not in rendered


def test_render_escapes_quotes_defensively() -> None:
    # event_name/category are validated identifiers, but the literal renderer must
    # still single-quote-escape — defense in depth (a future schema change
    # widening the event_name charset cannot turn into SQL injection).
    from shared.plugin_metrics import _sql_literal

    assert _sql_literal("a'b") == "'a''b'"


def test_render_agent_id_placeholder() -> None:
    query = "SELECT count(*) FROM events WHERE event_name = {event_name} AND {{agent_id}} AND $__timeFilter(ts)"
    spec = _spec(query=query, event_name="spawn", category="audit")
    assert render_query(spec) == (
        "SELECT count(*) FROM events WHERE event_name = 'spawn' AND {{agent_id}} AND $__timeFilter(ts)"
    )
    assert "agent_id = 7" in render_query(spec, agent_id=7)


def test_export_registry_keeps_templates_verbatim() -> None:
    query = (
        "SELECT count(*) FROM events WHERE event_name = {event_name} AND category = {category} "
        "AND {{agent_id}} AND $__timeFilter(ts)"
    )
    with PluginContext("ava_demo"):
        register_metric(_spec(name="agent_demo", query=query, output=["inspector"]))
    exported = export_registry()
    assert exported["schema_version"] == 1
    assert "exported_at" in exported
    assert len(exported["metrics"]) == 1
    metric = exported["metrics"][0]
    assert metric["name"] == "agent_demo"
    assert metric["plugin"] == "ava_demo"
    assert metric["query"] == query  # placeholders preserved for the gateway
    assert metric["output"] == ["inspector"]
