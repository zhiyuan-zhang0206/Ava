"""SQL safety validation + admission for plugin metrics (W13).

Covers what the data registry enforces when it admits a plugin's declared metrics: name
uniqueness and query safety per dialect — the static-SQL whitelist (single SELECT over
`events` / `agents_meta`, function / operator whitelist, no macros /
placeholders — task #180 PR C) and the LogQL contract
(``base/telemetry/metrics/logql.py``).
"""

import re
from typing import Any

import pytest
from pydantic import ValidationError

from base.packages.plugins.data_registry import DeclaredFace, build_data_registry
from base.packages.plugins.extensions import PluginContributions
from base.telemetry.metrics.plugin_metrics import (
    InvalidMetricQuery,
    MetricSpec,
    render_query,
    render_targets,
    render_title,
    validate_metric_sql,
    validate_spec_sql,
)

# ── SQL safety: accepted templates ────────────────────────────────────────────


def _good_sqls() -> list[str]:
    return [
        # plain static count (the core_live_agents shape)
        "SELECT count(*) AS calls FROM events WHERE event_name='llm_usage' "
        "AND category IN ('telemetry','log')",
        # FILTER + JSONB access + NULLIF division (static)
        "SELECT 100.0 * count(*) FILTER (WHERE attributes->>'status' = 'done') "
        '/ NULLIF(count(*), 0) AS "done %" FROM events '
        "WHERE event_name = 'task_update' AND category = 'audit' AND attributes ? 'status'",
        # subquery with its own FROM events (static)
        'SELECT agent_id, max(ratio) AS "max agent" FROM ('
        "SELECT agent_id, SUM((attributes->>'cache_read')::bigint)::numeric "
        "/ NULLIF(SUM((attributes->>'in_total')::bigint), 0) AS ratio "
        "FROM events WHERE event_name = 'llm_usage' AND category = 'telemetry' "
        "AND agent_id IS NOT NULL GROUP BY 1) per_agent "
        "GROUP BY 1 ORDER BY 1",
        # trailing semicolon tolerated
        "SELECT count(*) FROM events WHERE event_name = 'llm_usage';",
        # comparisons / arithmetic / IS NULL / LIMIT / OFFSET / bare columns
        "SELECT count(*) FROM events WHERE agent_id IS NULL LIMIT 10 OFFSET 5",
        # CASE expression + comparisons
        "SELECT count(*) FILTER (WHERE CASE WHEN event_name = 'a' THEN 1 ELSE 0 END = 1) FROM events",
        # ordered-set aggregate (W18): percentile_cont + WITHIN GROUP
        "SELECT percentile_cont(0.5) WITHIN GROUP "
        "(ORDER BY (attributes->>'duration_seconds')::float8) AS p50 FROM events",
        # the live agents_meta table (core_live_agents)
        "SELECT count(*) AS \"live agents\" FROM agents_meta WHERE status IN ('running','idling')",
        # keyword case-insensitivity (Tracker #766): the first keyword and the
        # table reference are accepted in any case, like every other keyword
        # check in the module
        "select count(*) from events where event_name = 'llm_usage' "
        "and category in ('telemetry', 'log')",
    ]


@pytest.mark.parametrize("sql", _good_sqls())
def test_validate_accepts_whitelisted_templates(sql: str) -> None:
    validate_metric_sql(sql)


def _bad_sqls() -> list[tuple[str, str]]:
    """(sql, expected fragment) — every query must be rejected and the
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
        ("EXPLAIN SELECT count(*) FROM events", "single SELECT"),
        ("SELECT count(*) FROM events WHERE event_name = 'x' OR 1=1 -- x", "comments"),
        # the template era is over (task #180 PR C): macros and placeholders
        # are rejected — the live event stream is read through LogQL
        ("SELECT count(*) FROM events WHERE $__timeFilter(ts)", "Grafana time macros"),
        (
            "SELECT $__timeGroup(ts, $__interval) AS time, count(*) FROM events GROUP BY 1",
            "Grafana time macros",
        ),
        ("SELECT count(*) FROM events WHERE event_name = {event_name}", "template placeholders"),
        ("SELECT count(*) FROM events WHERE category = {category}", "template placeholders"),
        ("SELECT count(*) FROM events WHERE {{agent_id}}", "template placeholders"),
        ("SELECT count(*) FROM events WHERE event_name = {event_named}", "template placeholders"),
    ]


@pytest.mark.parametrize(("sql", "fragment"), _bad_sqls())
def test_validate_rejects_malicious_templates(sql: str, fragment: str) -> None:
    with pytest.raises(InvalidMetricQuery, match=fragment):
        validate_metric_sql(sql)


# ── Task #882 core-migration constructs ─────────────────────────────────────


def test_validate_accepts_core_migration_sql() -> None:
    """The static SQL constructs the migrated core panels still need:
    generate_series in FROM with alias column list, LEFT JOIN of a subquery
    with ON, the agents_meta table, and the set/date helper functions —
    all without macros or placeholders (task #180 PR C)."""
    validate_metric_sql(
        "SELECT g.time AS time, coalesce(d.n, 0) AS n "
        "FROM generate_series(1, 10) AS g(time) "
        "LEFT JOIN ("
        "SELECT extract(epoch FROM ts)::bigint AS time, count(*) AS n FROM events "
        "WHERE event_name = 'delivery_stalled' GROUP BY 1"
        ") d ON d.time = g.time "
        "ORDER BY 1"
    )
    validate_metric_sql(
        "SELECT count(*) AS \"live agents\" FROM agents_meta WHERE status IN ('running','idling')"
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
        "WHERE event_name = 'llm_usage'"
    )
    validate_metric_sql("SELECT extract(epoch FROM ts) AS t FROM events")


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
        query="SELECT count(*) FROM events WHERE event_name = 'turn_end'",
        targets=[
            "SELECT count(*) FROM events WHERE category = 'telemetry'",
            "SELECT count(*) FROM events WHERE agent_id = 7",
        ],
    )
    # static SQL renders verbatim (the template era is over, task #180 PR C)
    rendered = render_targets(spec)
    assert rendered == [
        "SELECT count(*) FROM events WHERE event_name = 'turn_end'",
        "SELECT count(*) FROM events WHERE category = 'telemetry'",
        "SELECT count(*) FROM events WHERE agent_id = 7",
    ]


def test_validate_spec_sql_checks_all_targets() -> None:
    with pytest.raises(InvalidMetricQuery, match="not on the whitelist"):
        validate_spec_sql(
            MetricSpec(
                name="bad_target",
                title="Bad",
                event_name="x",
                category="log",
                query="SELECT count(*) FROM events",
                targets=["SELECT pg_sleep(1)"],
            )
        )


# ── admission ─────────────────────────────────────────────────────────────────────────


def _spec(**overrides: Any) -> MetricSpec:
    base: dict[str, Any] = {
        "name": "test_metric",
        "title": "Test Metric",
        "event_name": "turn_end",
        "category": "telemetry",
        "query": "SELECT count(*) FROM events WHERE event_name = 'turn_end' "
        "AND category = 'telemetry'",
    }
    base.update(overrides)
    return MetricSpec(**base)  # type: ignore[arg-type]


def _admit(*faces: tuple[str, list[MetricSpec]]) -> tuple[list[MetricSpec], list[str]]:
    """(admitted specs, refused plugin names) of a data registry built from `(plugin, specs)`."""
    registry, refused = build_data_registry(
        [DeclaredFace(plugin, PluginContributions(metrics=tuple(specs))) for plugin, specs in faces]
    )
    return list(registry.metrics()), refused


def test_admission_fills_plugin_from_the_registry_entry() -> None:
    admitted, refused = _admit(("ava_demo", [_spec()]))
    assert refused == []
    assert [(m.name, m.plugin) for m in admitted] == [("test_metric", "ava_demo")]


def test_admission_overrides_the_plugin_a_spec_claims() -> None:
    admitted, _ = _admit(("ava_demo", [_spec(plugin="someone_else")]))
    assert [m.plugin for m in admitted] == ["ava_demo"]


def test_a_second_plugin_claiming_a_metric_name_is_refused() -> None:
    admitted, refused = _admit(
        ("ava_demo", [_spec(name="dup")]), ("ava_other", [_spec(name="dup"), _spec(name="own")])
    )
    assert refused == ["ava_other"]
    assert [(m.name, m.plugin) for m in admitted] == [("dup", "ava_demo")]


def test_duplicate_name_within_one_plugin_is_refused() -> None:
    admitted, refused = _admit(("ava_demo", [_spec(name="dup"), _spec(name="dup")]))
    assert refused == ["ava_demo"]
    assert admitted == []


def test_a_refused_plugin_leaves_no_metrics_and_spares_the_others() -> None:
    bad = _spec(name="bad_target", targets=["SELECT pg_sleep(1)"])
    admitted, refused = _admit(
        ("ava_good", [_spec(name="good")]),
        ("ava_bad", [_spec(name="first_ok"), bad]),
        ("ava_after", [_spec(name="after")]),
    )
    assert refused == ["ava_bad"]
    assert [m.name for m in admitted] == ["good", "after"]


def test_spec_bad_event_name_rejected() -> None:
    with pytest.raises(ValidationError):
        _spec(event_name="TurnEnd")  # uppercase — must be ^[a-z][a-z0-9_-]*$
    with pytest.raises(ValidationError):
        _spec(event_name="turn end")
    with pytest.raises(ValidationError):
        _spec(event_name="turn-end!")


def test_admission_hyphenated_event_name_accepted() -> None:
    # The live event vocabulary carries hyphens (e.g. recall-filter), so the
    # event_name charset allows them; SQL templates are static now, so a
    # hyphenated event name cannot broaden the SQL surface at all.
    admitted, refused = _admit(("ava_demo", [_spec(event_name="recall-filter")]))
    assert refused == []
    (ok,) = admitted
    assert ok.event_name == "recall-filter"
    assert "event_name = 'turn_end'" in render_query(ok)


def test_spec_bad_category_rejected() -> None:
    with pytest.raises(ValidationError):
        _spec(category="metrics")  # not audit|telemetry|log


def test_admission_rejects_template_placeholders() -> None:
    # The template era is over (task #180 PR C): any SQL template carrying
    # {event_name}/{category}/{{agent_id}} placeholders is refused at
    # admission — the live event stream is read through LogQL, and the
    # {{agent_id}} ↔ grafana surface rule died with the placeholders.
    spec = _spec(
        query="SELECT count(*) FROM events WHERE event_name = {event_name} "
        "AND category = {category} AND {{agent_id}}"
    )
    with pytest.raises(InvalidMetricQuery, match="template placeholders"):
        validate_spec_sql(spec)
    admitted, refused = _admit(("ava_demo", [spec]))
    assert (admitted, refused) == ([], ["ava_demo"])


def test_spec_duplicate_output_surface_rejected() -> None:
    with pytest.raises(ValidationError):
        _spec(output=["grafana", "grafana"])


def test_spec_empty_output_rejected() -> None:
    with pytest.raises(ValidationError):
        _spec(output=[])


# ── rendering + export ────────────────────────────────────────────────────────


def test_render_static_sql_passes_through() -> None:
    # Static SQL renders verbatim — placeholders were retired with the
    # template cutover (task #180 PR C).
    (spec,) = _admit(("ava_demo", [_spec()]))[0]
    assert render_query(spec) == _spec().query


def test_render_escapes_quotes_defensively() -> None:
    # event_name/category are validated identifiers, but the literal renderer must
    # still single-quote-escape — defense in depth (a future schema change
    # widening the event_name charset cannot turn into SQL injection).
    from base.telemetry.metrics.plugin_metrics import _sql_literal

    assert _sql_literal("a'b") == "'a''b'"


# ── time basis: the registry renders the title suffix and the divisor ─────────


def _bucket(window: str) -> str:
    return (
        '(sum(count_over_time({service_name="unknown_service", event_name={event_name}} '
        f"| json [{window}]))"
    )


def _logql_spec(**overrides: Any) -> MetricSpec:
    base: dict[str, Any] = {"query_type": "logql", "query": _bucket("5m")}
    base.update(overrides)
    return _spec(**base)


def test_per_minute_divides_each_part_by_its_own_bucket() -> None:
    spec = _logql_spec(
        time_basis="per_minute",
        targets=[_bucket("30m")],
        target_names=["a", "b"],
    )
    assert render_title(spec) == "Test Metric (per minute)"
    first, second = render_targets(spec)
    assert first.endswith("[5m])) / 5")
    assert second.endswith("[30m])) / 30")
    assert render_query(spec, agent_id=3).endswith("[5m])) / 5")


def test_window_suffixes_the_title_and_leaves_the_query_alone() -> None:
    spec = _logql_spec(time_basis="window", query=_bucket("$__range"))
    assert render_title(spec) == "Test Metric (window)"
    assert render_query(spec) == _bucket("$__range").replace("{event_name}", '"turn_end"')


def test_the_basis_word_outside_a_parenthetical_is_just_a_title() -> None:
    spec = _logql_spec(time_basis="window", title="Context window", query=_bucket("$__range"))
    assert render_title(spec) == "Context window (window)"


def test_no_time_basis_renders_the_title_verbatim() -> None:
    assert render_title(_logql_spec()) == "Test Metric"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"time_basis": "per_minute", "title": "Calls (per minute)"}, "spells the time basis"),
        ({"time_basis": "window", "title": "Calls (window)"}, "spells the time basis"),
        ({"time_basis": "per_minute", "title": "Cost (USD, per minute)"}, "spells the time basis"),
        ({"time_basis": "per_minute", "query": _bucket("$__range")}, "exactly one [Nm]"),
        (
            {"time_basis": "per_minute", "query": _bucket("5m") + " + " + _bucket("5m")},
            "exactly one [Nm]",
        ),
        ({"time_basis": "per_minute", "query": _bucket("5m") + " / 5"}, "already divides"),
        ({"time_basis": "window", "query": _bucket("5m")}, "$__range"),
    ],
)
def test_time_basis_refuses_a_hand_spelled_or_unrenderable_spec(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=re.escape(message)):
        _logql_spec(**overrides)


def test_per_minute_needs_a_bucketed_dialect() -> None:
    with pytest.raises(ValidationError, match="logql or promql"):
        _spec(time_basis="per_minute")
