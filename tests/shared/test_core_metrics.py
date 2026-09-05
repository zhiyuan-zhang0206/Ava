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


def _spec(name: str = "core_test", **overrides) -> MetricSpec:
    query = overrides.pop("query", None) or (  # pyright: ignore[reportUnknownMemberType]
        "SELECT count(*) AS n FROM events WHERE event_name = 'turn_end' AND category = 'telemetry'"
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
    # The template era is over (task #180 PR C): {{agent_id}} in a SQL query
    # is rejected outright — the per-agent inspector idiom lives in the LogQL
    # dialect (rendered per agent), not in SQL templates.
    with pytest.raises(InvalidMetricQuery, match="template placeholders"):
        core_metrics.register_core_metric(
            _spec(
                name="core_agent",
                query="SELECT count(*) FROM events WHERE {{agent_id}}",
                output=["inspector"],
            )
        )


def test_collect_core_metrics_includes_statistics_coverage() -> None:
    """The core registry includes every statistic surfaced to operators."""
    import sys

    for module_name in core_metrics._CORE_DEFINITION_MODULES:
        sys.modules.pop(module_name, None)
    specs = core_metrics.collect_core_metrics()
    assert specs
    assert all(s.plugin == "core" for s in specs)
    by_name = {spec.name: spec for spec in specs}
    assert set(by_name) >= {
        "ava_obs_llm_cost_usd",
        "ava_obs_events_rate",
        "core_llm_input_tokens_24h",
        "core_llm_output_tokens_24h",
        "core_cache_hit_rate_24h",
        "core_avg_turn_duration_24h",
    }
    assert by_name["core_cache_hit_rate_24h"].field_defaults == {"decimals": 2}
    avg_turn = by_name["core_avg_turn_duration_24h"].query
    assert avg_turn.count('attributes_ok="true"') == 2
    assert "sum(count_over_time(" in avg_turn
    assert by_name["core_unresolved_warning"].query_type == "promql"
    assert (
        by_name["core_unresolved_warning"].query
        == "ava_resolution_status_unresolved_warnings_ratio"
    )
    assert by_name["core_unresolved_error"].query_type == "promql"
    assert by_name["core_unresolved_error"].query == "ava_resolution_status_unresolved_errors_ratio"


# ── LogQL dialect (task #1280) ────────────────────────────────────────────────


def _logql_spec(name: str = "core_loki", **overrides: Any) -> MetricSpec:
    query = overrides.pop("query", None) or (
        'sum(count_over_time({service_name="unknown_service", event_name={event_name}} | json | '
        'category=~"{category_re}|log" [$__range]))'
    )
    return MetricSpec(
        name=name,
        title="Core Loki",
        event_name="llm_usage",
        category="telemetry",
        query_type="logql",
        query=query,
        **overrides,
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


def test_logql_rejects_an_untemplated_cross_category_filter() -> None:
    """Class resolution uses Prometheus gauges, not a raw union query."""
    with pytest.raises(InvalidMetricQuery, match="placeholders"):
        core_metrics.register_core_metric(
            _logql_spec(
                name="core_unresolved_events",
                query=(
                    'sum(count_over_time({service_name="unknown_service"} | json | '
                    'category=~"telemetry|log" | level="warning" [$__range]))'
                ),
            )
        )


def test_render_logql_quotes_and_agent_placeholder() -> None:
    """LogQL rendering: double-quoted literals (SQL single quotes would be a
    parse error), {category_re} unquoted inside a regex, {{agent_id}} as a
    label filter."""
    spec = _logql_spec(
        name="core_loki_render",
        query=(
            'sum(count_over_time({service_name="unknown_service", event_name={event_name}} | json | '
            'category=~"{category_re}|log" | '
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
    # event_name is a promoted stream label (2026-08-23 cutover): the matcher
    # renders into the selector, before the | json stage
    selector = rendered.split("| json")[0]
    assert 'event_name="llm_usage"' in selector
    assert "event_name" not in rendered.split("| json")[1]
