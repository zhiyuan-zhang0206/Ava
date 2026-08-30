"""Core observability metrics pack tests (Task #882 migration, #1280 Loki).

Covers the generic observability pack migrated from the retired
``ava_observability`` plugin to core metrics: registration shape (19 metrics
across grafana / inspector surfaces, plugin == "core"), the LogQL template
safety validation, and every rendered query's structure — the stream selector,
the ``| json`` pipeline, the event_name/category placeholders, and the
per-panel filter semantics (exec spellings, syntax-fix kinds, halt classes,
spawner sources, table top-k shapes, agent_id rendering). Turn duration is the
one PromQL exception: it shares the R18 histogram quantiles. The queries
themselves were verified against the live Loki stream during the migration
(task #1280); unit tests lock the registry shape and rendered templates instead
of executing them (there is no Loki in the test environment).
"""

from __future__ import annotations

import pytest

from shared import core_metrics
from shared.metrics_logql import validate_logql
from shared.plugin_metrics import InvalidMetricQuery, render_query, render_targets

EXPECTED = {
    # name -> (panel, output, event_name, category, n_targets)
    "ava_obs_llm_cost_usd": ("timeseries", ["grafana"], "llm_usage", "telemetry", 1),
    "ava_obs_agent_llm_cost_usd": ("timeseries", ["inspector"], "llm_usage", "telemetry", 1),
    "ava_obs_llm_error_rate": ("timeseries", ["grafana", "inspector"], "llm_usage", "telemetry", 4),
    "ava_obs_turn_ok_rate": ("timeseries", ["grafana", "inspector"], "turn_end", "telemetry", 1),
    "ava_obs_turn_duration_s": ("timeseries", ["grafana"], "turn_end", "telemetry", 2),
    "ava_obs_exec_success_rate": ("timeseries", ["grafana"], "exec", "telemetry", 6),
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


def _load_pack() -> None:
    """Import the core observability module (fresh core registry each call)."""
    import importlib
    import sys

    core_metrics.clear_core_registry()
    module_name = "shared.core_metrics_observability"
    if module_name in sys.modules:
        del sys.modules[module_name]
    importlib.import_module(module_name)


def _all_rendered() -> dict[str, list[str]]:
    """name -> every rendered expr (primary + targets), placeholders filled."""
    return {
        spec.name: [render_query(spec), *render_targets(spec)[1:]]
        for spec in core_metrics.registered_core_metrics()
    }


# ── registration ──────────────────────────────────────────────────────────────


def test_pack_registers_all_metrics() -> None:
    _load_pack()
    specs = {m.name: m for m in core_metrics.registered_core_metrics()}
    assert set(EXPECTED) == set(specs)
    for name, (panel, output, event_name, category, n_targets) in EXPECTED.items():
        spec = specs[name]
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
        if spec.query_type == "logql":
            for expr in [render_query(spec), *render_targets(spec)[1:]]:
                validate_logql(expr, name)


def test_logql_queries_have_event_stream_and_json() -> None:
    """Every LogQL query selects the event stream and pipelines | json."""
    _load_pack()
    for name, exprs in _all_rendered().items():
        if name == "ava_obs_turn_duration_s":
            continue
        for expr in exprs:
            assert 'service_name="unknown_service"' in expr, name
            assert "| json" in expr, name
            assert "{event_name}" not in expr and "{category}" not in expr, name
            # event_name/agent_id are promoted stream labels: their matchers
            # must sit in the stream selector ({... event_name=...}), never as
            # `| event_name` pipeline filters (task #1467). The {{agent_id}}
            # inspector placeholder is the one allowed pipeline exception —
            # the gateway renders it per agent.
            assert "| event_name" not in expr, f"{name}: event_name filter after | json:\n{expr}"
            assert "| agent_id" not in expr.replace("| {{agent_id}}", ""), (
                f"{name}: agent_id filter after | json:\n{expr}"
            )


def test_logql_template_validation_rejects_drift() -> None:
    """The template-form validator refuses: a query that lost the stream
    selector, one without the json pipeline, and one that hardcodes an
    event filter instead of the placeholders."""
    from shared.metrics_logql import _validate_logql_template

    with pytest.raises(InvalidMetricQuery, match="stream"):
        _validate_logql_template(
            'sum(count_over_time({other="x"} | json | event_name={event_name} [5m]))',
            "t",
        )
    with pytest.raises(InvalidMetricQuery, match="json"):
        _validate_logql_template(
            'sum(count_over_time({service_name="unknown_service"} | event_name={event_name} [5m]))',
            "t",
        )
    with pytest.raises(InvalidMetricQuery, match="placeholders"):
        _validate_logql_template(
            'sum(count_over_time({service_name="unknown_service"} | json | event_name="delivery_stalled" [5m]))',
            "t",
        )


# ── cost via the payload cost_usd field (task #2626 / #1280) ─────────────────


def test_cost_queries_unwrap_cost_usd() -> None:
    """Cost panels unwrap attributes_cost_usd from the payload (producer-side
    catalog pricing, #2626) — the SQL CASE mirroring model rates is gone."""
    _load_pack()
    specs = {m.name: m for m in core_metrics.registered_core_metrics()}
    for name in ("ava_obs_llm_cost_usd", "ava_obs_agent_llm_cost_usd"):
        query = specs[name].query
        assert "unwrap attributes_cost_usd" in query, name
        assert "CASE" not in query, name
        assert "attributes->>'model'" not in query, name


# ── per-panel filter semantics (rendered-template locks) ─────────────────────


def test_exec_breakdown_covers_legacy_spellings() -> None:
    """exec panel series: ok / failed / timeout / cancelled / node_timeout /
    other — parenthesized legacy spellings counted via RE2 character classes,
    unknown exec* events fall into other (legacy exec_thread_stuck rows now
    land in other — the thread backend stopped emitting them, PR3)."""
    _load_pack()
    exprs = _all_rendered()["ava_obs_exec_success_rate"]
    # event_name is a promoted stream label (2026-08-23 cutover, task #1467):
    # every matcher sits in the stream selector, before the | json stage.
    assert all("event_name=" in e.split("| json")[0] for e in exprs), exprs
    assert 'event_name="exec"' in exprs[0]
    assert 'event_name=~"exec_failed|exec[(]failed[)]"' in exprs[1]
    assert 'event_name=~"exec_timeout|exec[(]timeout[)]"' in exprs[2]
    assert 'event_name=~"exec_cancelled|exec[(]cancelled[)]"' in exprs[3]
    assert 'event_name="exec_node_timeout"' in exprs[4]
    # other: exec prefix minus every known spelling. Selector matchers are
    # full-string regexes, so the negated list excludes exactly the named
    # spellings (the pre-migration pipeline form was substring-based and
    # matched nothing — exec.* rows all contain "exec").
    assert 'event_name=~"exec.*"' in exprs[5]
    assert "exec_node_timeout" in exprs[5]
    assert "exec_thread_stuck" not in exprs[5]


def test_turn_ok_rate_math_shape() -> None:
    _load_pack()
    expr = _all_rendered()["ava_obs_turn_ok_rate"][0]
    assert "100 * sum(count_over_time(" in expr
    assert 'attributes_ok="true"' in expr
    assert expr.count("sum(count_over_time(") == 2  # ok / total


def test_turn_duration_uses_the_alert_histogram_quantiles() -> None:
    """The dashboard follows R18's Prometheus p95 with a p50 companion."""
    _load_pack()
    spec = {metric.name: metric for metric in core_metrics.registered_core_metrics()}[
        "ava_obs_turn_duration_s"
    ]
    assert spec.query_type == "promql"
    assert render_targets(spec) == [
        "histogram_quantile(0.95, sum by (le) (rate(ava_turn_end_duration_seconds_bucket[10m])))",
        "histogram_quantile(0.5, sum by (le) (rate(ava_turn_end_duration_seconds_bucket[10m])))",
    ]
    assert spec.target_names == ["p95_s", "p50_s"]


def test_halt_breakdown_buckets() -> None:
    _load_pack()
    exprs = _all_rendered()["ava_obs_halt_breakdown"]
    assert 'attributes_body="no tool_call (idle)"' in exprs[0]
    assert 'attributes_body=~".*compact.*"' in exprs[1]
    assert 'attributes_body=~"lifecycle .*"' in exprs[2]
    # other: none of the above (missing body matches the != / !~ filters)
    assert 'attributes_body!="no tool_call (idle)"' in exprs[3]
    assert 'attributes_body!~".*compact.*"' in exprs[3]
    assert 'attributes_body!~"lifecycle .*"' in exprs[3]


def test_syntax_fix_kind_buckets() -> None:
    _load_pack()
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
    # other: !~ over the whole known-kind alternation (missing label matches)
    assert (
        'attributes_fixes!~".*(ruff|invalid_escape|missing_imports|chinese_punct|bracket_matching).*"'
        in exprs[6]
    )


def test_lifecycle_and_spawner_window_aggregates() -> None:
    _load_pack()
    spawner = _all_rendered()["ava_obs_spawn_by_spawner"]
    # Per-minute normalization: bucketed at $__interval, divided by the
    # bucket width in minutes so every bar is a rate (FleetView bucket
    # transparency pass).
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
    _load_pack()
    expr = _all_rendered()["ava_obs_sdk_call_top"][0]
    assert expr.startswith("topk(20, sum by (attributes_fn) (count_over_time(")
    assert "$__range" in expr  # instant query over the whole window
    assert expr.endswith("[$__range])) * 10)")


def test_events_rate_uses_rate() -> None:
    _load_pack()
    expr = _all_rendered()["ava_obs_events_rate"][0]
    assert expr == 'sum(rate({service_name="unknown_service"} | json | __error__="" [1m]))'


def test_frontend_table_shapes() -> None:
    _load_pack()
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
    """Four instant targets (calls / in / out / cost), all per-agent, all
    over the whole window; the cost column unwraps cost_usd."""
    _load_pack()
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
    """{{agent_id}} renders to agent_id="<n>" (LogQL label filter) and the
    rendered query passes the rendered-form validation."""
    _load_pack()
    for name in ("ava_obs_agent_llm_cost_usd", "ava_obs_agent_delivery_stalled_count"):
        spec = next(m for m in core_metrics.registered_core_metrics() if m.name == name)
        rendered = render_query(spec, agent_id=1234)
        assert 'agent_id="1234"' in rendered
        assert "{{agent_id}}" not in rendered
        validate_logql(rendered, name)
