from __future__ import annotations

import pytest

from base.telemetry.metrics.core import catalog, exec_envelope, frontend, observability
from base.telemetry.metrics.logql import validate_logql
from base.telemetry.metrics.plugin_metrics import (
    InvalidMetricQuery,
    MetricSpec,
    render_query,
    render_targets,
)

EXPECTED = {
    "ava_obs_llm_cost_usd": ("timeseries", ["grafana"], "llm_usage", "telemetry", 1),
    "ava_obs_agent_llm_cost_usd": ("timeseries", ["inspector"], "llm_usage", "telemetry", 1),
    "ava_obs_llm_error_rate": ("timeseries", ["grafana", "inspector"], "llm_usage", "telemetry", 2),
    "ava_obs_turn_ok_rate": ("timeseries", ["grafana", "inspector"], "turn_end", "telemetry", 1),
    "ava_obs_turn_duration_s": ("timeseries", ["grafana"], "turn_end", "telemetry", 2),
    "ava_obs_compaction_summary_history_ratio": (
        "timeseries",
        ["grafana"],
        "compaction_completed",
        "telemetry",
        1,
    ),
    "ava_obs_compaction_rate": (
        "timeseries",
        ["grafana"],
        "compaction_completed",
        "telemetry",
        5,
    ),
    "ava_obs_compaction_soft_share": ("stat", ["grafana"], "compaction_completed", "telemetry", 2),
    "ava_obs_exec_success_rate": ("timeseries", ["grafana"], "exec", "telemetry", 6),
    "ava_obs_exec_envelope_size": ("timeseries", ["grafana"], "exec_envelope", "telemetry", 3),
    "ava_obs_exec_envelope_serialize": (
        "timeseries",
        ["grafana"],
        "exec_envelope",
        "telemetry",
        3,
    ),
    "ava_obs_syntax_fix_by_kind": ("timeseries", ["grafana"], "syntax_fix", "telemetry", 7),
    "ava_obs_spawn_by_spawner": ("barchart", ["grafana"], "spawn", "audit", 1),
    "ava_obs_lifecycle_counts": ("barchart", ["grafana"], "spawn", "audit", 1),
    "ava_obs_sdk_call_top": ("table", ["grafana"], "sdk_call", "telemetry", 1),
    "ava_obs_agent_llm_usage_table": ("table", ["grafana"], "llm_usage", "telemetry", 4),
    "ava_obs_halt_breakdown": ("timeseries", ["grafana"], "halt", "telemetry", 4),
    "ava_obs_delivery_stalled_count": (
        "stat",
        ["grafana"],
        "delivery_stalled",
        "telemetry",
        1,
    ),
    "ava_obs_agent_delivery_stalled_count": (
        "timeseries",
        ["inspector"],
        "delivery_stalled",
        "telemetry",
        1,
    ),
    "ava_obs_events_rate": ("timeseries", ["grafana"], "log", "log", 1),
    "ava_obs_frontend_interactions": (
        "timeseries",
        ["grafana"],
        "frontend_interaction",
        "telemetry",
        1,
    ),
    "ava_obs_frontend_top_elements": ("table", ["grafana"], "frontend_interaction", "telemetry", 1),
    "ava_obs_frontend_page_views": ("table", ["grafana"], "frontend_interaction", "telemetry", 1),
    "ava_obs_frontend_settings_changes": (
        "table",
        ["grafana"],
        "frontend_interaction",
        "telemetry",
        1,
    ),
}


def _pack() -> list[MetricSpec]:
    return [
        catalog.validate_core_metric(spec)
        for module in (observability, exec_envelope, frontend)
        for spec in module.core_metrics()
    ]


def _all_rendered() -> dict[str, list[str]]:
    return {spec.name: [render_query(spec), *render_targets(spec)[1:]] for spec in _pack()}


def _assert_spec_matches_expected(name: str, spec: MetricSpec) -> None:
    panel, output, event_name, category, n_targets = EXPECTED[name]
    assert spec.panel == panel, name
    assert spec.output == output, name
    assert spec.event_name == event_name, name
    assert spec.category == category, name
    assert spec.plugin == "core", name
    expected_query_type = "promql" if name == "ava_obs_turn_duration_s" else "logql"
    assert spec.query_type == expected_query_type, name
    assert len(spec.targets or []) + 1 == n_targets, name
    if spec.target_names is not None:
        assert len(spec.target_names) == n_targets, name


def _assert_rendered_logql_validates(name: str, spec: MetricSpec) -> None:
    for expr in [render_query(spec), *render_targets(spec)[1:]]:
        validate_logql(expr, name)


def test_pack_registers_all_metrics() -> None:
    specs = {m.name: m for m in _pack()}
    assert set(EXPECTED) == set(specs)
    for name, spec in specs.items():
        _assert_spec_matches_expected(name, spec)
        if spec.query_type == "logql":
            _assert_rendered_logql_validates(name, spec)


def test_logql_queries_have_event_stream_and_json() -> None:
    for name, exprs in _all_rendered().items():
        if name == "ava_obs_turn_duration_s":
            continue
        for expr in exprs:
            assert 'service_name="unknown_service"' in expr, name
            assert "| json" in expr, name
            assert "{event_name}" not in expr and "{category}" not in expr, name
            assert "| event_name" not in expr, f"{name}: event_name filter after | json:\n{expr}"
            assert "| agent_id" not in expr.replace("| {{agent_id}}", ""), (
                f"{name}: agent_id filter after | json:\n{expr}"
            )


def test_logql_template_validation_rejects_drift() -> None:
    from base.telemetry.metrics.logql import validate_logql_template

    with pytest.raises(InvalidMetricQuery, match="stream"):
        validate_logql_template(
            'sum(count_over_time({other="x"} | json | event_name={event_name} [5m]))',
            "t",
        )
    with pytest.raises(InvalidMetricQuery, match="json"):
        validate_logql_template(
            'sum(count_over_time({service_name="unknown_service"} | event_name={event_name} [5m]))',
            "t",
        )
    with pytest.raises(InvalidMetricQuery, match="placeholders"):
        validate_logql_template(
            'sum(count_over_time({service_name="unknown_service"} | json | event_name="delivery_stalled" [5m]))',
            "t",
        )


def test_cost_queries_unwrap_cost_usd() -> None:
    specs = {m.name: m for m in _pack()}
    for name in ("ava_obs_llm_cost_usd", "ava_obs_agent_llm_cost_usd"):
        query = specs[name].query
        assert "unwrap attributes_cost_usd" in query, name
        assert "CASE" not in query, name
        assert "attributes->>'model'" not in query, name


def test_exec_breakdown_covers_every_outcome() -> None:
    exprs = _all_rendered()["ava_obs_exec_success_rate"]
    assert all("event_name=" in e.split("| json")[0] for e in exprs), exprs
    assert 'event_name="exec"' in exprs[0]
    assert 'event_name="exec_failed"' in exprs[1]
    assert 'event_name="exec_timeout"' in exprs[2]
    assert 'event_name="exec_cancelled"' in exprs[3]
    assert 'event_name="exec_node_timeout"' in exprs[4]
    assert (
        'event_name=~"exec.*", '
        'event_name!~"exec|exec_envelope|exec_failed|exec_timeout|'
        'exec_cancelled|exec_node_timeout"'
    ) in exprs[5]


def test_turn_ok_rate_math_shape() -> None:
    expr = _all_rendered()["ava_obs_turn_ok_rate"][0]
    assert "100 * sum(count_over_time(" in expr
    assert 'attributes_ok="true"' in expr
    assert expr.count("sum(count_over_time(") == 2


def test_turn_duration_uses_the_alert_histogram_quantiles() -> None:
    spec = {metric.name: metric for metric in _pack()}["ava_obs_turn_duration_s"]
    assert spec.query_type == "promql"
    assert render_targets(spec) == [
        "histogram_quantile(0.95, sum by (le) (rate(ava_turn_end_duration_seconds_bucket[10m])))",
        "histogram_quantile(0.5, sum by (le) (rate(ava_turn_end_duration_seconds_bucket[10m])))",
    ]
    assert spec.target_names == ["p95_s", "p50_s"]


def test_compaction_panels_measure_applied_ratio_and_frequency() -> None:
    rendered = _all_rendered()
    ratio = rendered["ava_obs_compaction_summary_history_ratio"][0]
    rate = rendered["ava_obs_compaction_rate"][0]

    assert "unwrap attributes_summary_history_ratio" in ratio
    assert ratio.startswith("avg(100 * avg_over_time(")
    assert "100 * avg_over_time" in ratio
    assert 'event_name="compaction_completed"' in ratio
    assert 'event_name="compaction_completed"' in rate
    assert "sum(count_over_time(" in rate
    assert rate.endswith(" / 5")


def test_halt_breakdown_buckets() -> None:
    exprs = _all_rendered()["ava_obs_halt_breakdown"]
    assert 'attributes_body="no tool_call (idle)"' in exprs[0]
    assert 'attributes_body=~".*compact.*"' in exprs[1]
    assert 'attributes_body=~"lifecycle .*"' in exprs[2]
    assert 'attributes_body!="no tool_call (idle)"' in exprs[3]
    assert 'attributes_body!~".*compact.*"' in exprs[3]
    assert 'attributes_body!~"lifecycle .*"' in exprs[3]


def test_syntax_fix_kind_buckets() -> None:
    exprs = _all_rendered()["ava_obs_syntax_fix_by_kind"]
    assert 'attributes_fixes=~".*ruff_format.*"' in exprs[0]
    assert 'attributes_fixes=~".*ruff.*" | attributes_fixes!~".*ruff_format.*"' in exprs[1]
    for needle, idx in [
        ("invalid_escape", 2),
        ("missing_imports", 3),
        ("chinese_punct", 4),
        ("bracket_matching", 5),
    ]:
        assert f'attributes_fixes=~".*{needle}.*"' in exprs[idx], needle
    assert (
        'attributes_fixes!~".*(ruff|invalid_escape|missing_imports|chinese_punct|bracket_matching).*"'
        in exprs[6]
    )


def test_lifecycle_and_spawner_window_aggregates() -> None:
    spawner = _all_rendered()["ava_obs_spawn_by_spawner"]
    assert spawner == [
        'sum by (source) (count_over_time({service_name="unknown_service", event_name="spawn"} | json | '
        'category="audit" [$__interval])) / ($__interval_ms / 60000)'
    ]
    life = _all_rendered()["ava_obs_lifecycle_counts"]
    assert life == [
        'sum by (event_name) (count_over_time({service_name="unknown_service", event_name=~"^(spawn|terminate|restart|resurrect|fork)$"} | json | '
        'category="audit" '
        "[$__interval])) / ($__interval_ms / 60000)"
    ]


def test_sdk_call_top_table_shape() -> None:
    expr = _all_rendered()["ava_obs_sdk_call_top"][0]
    assert expr.startswith("topk(20, sum by (attributes_fn) (sum_over_time(")
    assert "$__range" in expr
    assert 'unwrap attributes_sample_rate | __error__=""' in expr
    assert "* 10" not in expr


def test_events_rate_uses_rate() -> None:
    expr = _all_rendered()["ava_obs_events_rate"][0]
    assert expr == 'sum(rate({service_name="unknown_service"} | json | __error__="" [1m]))'


def test_frontend_table_shapes() -> None:
    rendered = _all_rendered()
    assert rendered["ava_obs_frontend_top_elements"][0].startswith(
        "topk(15, sum by (attributes_element) (count_over_time("
    )
    assert 'attributes_element="page-view"' in rendered["ava_obs_frontend_page_views"][0]
    assert 'attributes_element="setting-change"' in rendered["ava_obs_frontend_settings_changes"][0]
    for name in (
        "ava_obs_frontend_top_elements",
        "ava_obs_frontend_page_views",
        "ava_obs_frontend_settings_changes",
    ):
        assert 'source="user"' in rendered[name][0], name


def test_agent_llm_usage_table_columns() -> None:
    exprs = _all_rendered()["ava_obs_agent_llm_usage_table"]
    assert len(exprs) == 4
    assert "sum by (agent_id) (count_over_time(" in exprs[0]
    assert "unwrap attributes_in_total" in exprs[1]
    assert "unwrap attributes_out_total" in exprs[2]
    assert "unwrap attributes_cost_usd" in exprs[3]
    for expr in exprs:
        assert "agent_id!=" in expr
        assert "$__range" in expr


def test_inspector_agent_queries_render_agent_id() -> None:
    for name in ("ava_obs_agent_llm_cost_usd", "ava_obs_agent_delivery_stalled_count"):
        spec = next(m for m in _pack() if m.name == name)
        rendered = render_query(spec, agent_id=1234)
        assert 'agent_id="1234"' in rendered
        assert "{{agent_id}}" not in rendered
        validate_logql(rendered, name)


def test_soft_compaction_share_excludes_manual_and_unattributed_history() -> None:
    rendered = _all_rendered()
    share = rendered["ava_obs_compaction_soft_share"][0]
    assert 'attributes_compact_kind="compact_summary"' in share
    assert 'attributes_compact_kind=~"compact_summary|auto"' in share
    assert 'attributes_reminder_shown=~"true|false"' in share
    assert "compact_request" not in share
    assert " or (0 * sum(count_over_time(" in share
    assert "$__range" in share


def test_preparation_target_requires_a_reminder_in_the_completed_window() -> None:
    reminded = _all_rendered()["ava_obs_compaction_soft_share"][1]
    assert 'attributes_reminder_shown="true"' in reminded
    assert 'attributes_compact_kind="compact_summary"' in reminded
    assert 'attributes_compact_kind=~"compact_summary|auto"' in reminded
    assert " or (0 * sum(count_over_time(" in reminded


def test_completed_paths_separate_reminder_coverage_from_hard_fallback() -> None:
    paths = _all_rendered()["ava_obs_compaction_rate"][1:]
    assert len(paths) == 4
    for query, (kind, shown) in zip(
        paths,
        (
            ("compact_summary", "true"),
            ("compact_summary", "false"),
            ("auto", "true"),
            ("auto", "false"),
        ),
        strict=True,
    ):
        assert f'attributes_compact_kind="{kind}"' in query
        assert f'attributes_reminder_shown="{shown}"' in query
