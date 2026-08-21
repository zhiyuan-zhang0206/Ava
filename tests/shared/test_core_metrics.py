"""Core metrics registry (Task #882) — registration, validation reuse, and
in-process collection shared with the plugin registry."""

from typing import Any

import pytest

from shared import core_metrics
from shared.plugin_metrics import (
    DuplicateMetric,
    InvalidMetricQuery,
    MetricSpec,
    clear_registry,
    render_query,
)


@pytest.fixture(autouse=True)
def _clean():
    core_metrics.clear_core_registry()
    clear_registry()
    yield
    core_metrics.clear_core_registry()
    clear_registry()


def _spec(name: str = "core_test", **overrides) -> MetricSpec:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    query = overrides.pop("query", None) or (  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        "SELECT $__timeGroup(ts, $__interval) AS time, count(*) AS n "
        "FROM events WHERE event_name = {event_name} AND category = {category} "
        "AND $__timeFilter(ts) GROUP BY 1 ORDER BY 1"
    )
    return MetricSpec(
        name=name,
        title="Core Test",
        event_name="turn_end",
        category="telemetry",
        query=query,  # pyright: ignore[reportUnknownArgumentType]
        **overrides,  # pyright: ignore[reportUnknownArgumentType]
    )


def test_register_core_metric_sets_plugin_core() -> None:
    filled = core_metrics.register_core_metric(_spec())
    assert filled.plugin == "core"
    assert [m.name for m in core_metrics.registered_core_metrics()] == ["core_test"]


def test_register_core_metric_rejects_duplicates() -> None:
    core_metrics.register_core_metric(_spec())
    with pytest.raises(DuplicateMetric):
        core_metrics.register_core_metric(_spec())


def test_register_core_metric_validates_sql_like_plugins() -> None:
    # unknown function -> rejected at register time (same validator as plugins)
    with pytest.raises(InvalidMetricQuery, match="not on the whitelist"):
        core_metrics.register_core_metric(
            _spec(name="core_bad", query="SELECT version() FROM events")
        )
    # multi-target specs are validated per target
    with pytest.raises(InvalidMetricQuery, match="not on the whitelist"):
        core_metrics.register_core_metric(
            _spec(
                name="core_bad_target",
                targets=["SELECT pg_sleep(1)"],
            )
        )


def test_register_core_metric_agent_placeholder_rule() -> None:
    with pytest.raises(InvalidMetricQuery, match="placeholder"):
        core_metrics.register_core_metric(
            _spec(
                name="core_agent",
                query="SELECT count(*) FROM events WHERE {{agent_id}}",
                output=["grafana"],
            )
        )
    # inspector-only is fine
    spec = core_metrics.register_core_metric(
        _spec(
            name="core_agent_ok",
            query="SELECT count(*) FROM events WHERE {{agent_id}}",
            output=["inspector"],
        )
    )
    assert "inspector" in spec.output


def test_collect_core_metrics_tolerates_missing_modules() -> None:
    # core_metrics_panels does not exist in this worktree yet — a missing
    # definition module is skipped (no raise) while the present modules
    # (core_metrics_observability, the migrated ava_observability pack)
    # still register and render as core metrics. The module cache is reset
    # first: the autouse fixture cleared the registry, and a cached module
    # would not re-register (collect's import-once semantics are a fresh-
    # process behavior, as in the generator).
    import sys

    sys.modules.pop("shared.core_metrics_observability", None)
    specs = core_metrics.collect_core_metrics()
    assert specs
    assert all(s.plugin == "core" for s in specs)
    assert {s.name for s in specs} >= {"ava_obs_llm_cost_usd", "ava_obs_events_rate"}


# ── LogQL dialect (task #1280) ────────────────────────────────────────────────


def _logql_spec(name: str = "core_loki", **overrides: Any) -> MetricSpec:
    query = overrides.pop("query", None) or (
        'sum(count_over_time({service_name="unknown_service"} | json | '
        'category=~"{category_re}|log" | event_name={event_name} [$__range]))'
    )
    return MetricSpec(
        name=name,
        title="Core Loki",
        event_name="llm_usage",
        category="telemetry",
        query_type="logql",
        query=query,  # pyright: ignore[reportUnknownArgumentType]
        **overrides,  # pyright: ignore[reportUnknownArgumentType]
    )


def test_register_core_metric_validates_logql() -> None:
    """LogQL templates are validated against the stream-selector / | json /
    placeholder contract, not the SQL whitelist."""
    with pytest.raises(InvalidMetricQuery, match="stream"):
        core_metrics.register_core_metric(
            _logql_spec(
                name="core_loki_bad_selector",
                query='sum(count_over_time({other="x"} | json | event_name={event_name} [5m]))',
            )
        )
    with pytest.raises(InvalidMetricQuery, match="json"):
        core_metrics.register_core_metric(
            _logql_spec(
                name="core_loki_no_json",
                query=(
                    'sum(count_over_time({service_name="unknown_service"} | '
                    "event_name={event_name} [5m]))"
                ),
            )
        )
    with pytest.raises(InvalidMetricQuery, match="placeholders"):
        core_metrics.register_core_metric(
            _logql_spec(
                name="core_loki_hardcoded",
                query=(
                    'sum(count_over_time({service_name="unknown_service"} | json | '
                    'event_name="delivery_stalled" [5m]))'
                ),
            )
        )
    # a whole-stream query (no event filter at all) is legitimate
    spec = core_metrics.register_core_metric(
        _logql_spec(
            name="core_loki_whole_stream",
            query='sum(rate({service_name="unknown_service"} | json [$__interval]))',
        )
    )
    assert spec.query_type == "logql"


def test_render_logql_quotes_and_agent_placeholder() -> None:
    """LogQL rendering: double-quoted literals (SQL single quotes would be a
    parse error), {category_re} unquoted inside a regex, {{agent_id}} as a
    label filter."""
    spec = _logql_spec(
        name="core_loki_render",
        query=(
            'sum(count_over_time({service_name="unknown_service"} | json | '
            'category=~"{category_re}|log" | event_name={event_name} | '
            "{{agent_id}} [$__interval]))"
        ),
        output=["inspector"],
    )
    core_metrics.register_core_metric(spec)
    rendered = render_query(spec, agent_id=42)
    assert 'category=~"telemetry|log"' in rendered
    assert 'event_name="llm_usage"' in rendered
    assert 'agent_id="42"' in rendered
    assert "{event_name}" not in rendered
