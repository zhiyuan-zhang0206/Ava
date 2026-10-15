from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from base.packages.dashboard_supply import load_repo_plugin_specs
from base.telemetry.metrics.core import catalog
from base.telemetry.metrics.plugin_metrics import (
    MetricSpec,
    render_query,
    render_targets,
    render_title,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def registered_metrics() -> list[MetricSpec]:
    result = load_repo_plugin_specs()
    assert result.failed == []
    return result.specs


def _load_core() -> list[MetricSpec]:
    return catalog.collect_core_metrics()


def test_shipped_plugin_metrics_are_logql() -> None:
    specs = registered_metrics()
    assert len(specs) == 11
    names = {spec.name for spec in specs}
    assert {
        "ava_memory_recall_search_latency_ms",
        "ava_memory_recall_injected_notes",
    } <= names
    for spec in specs:
        assert spec.query_type == "logql", f"{spec.name} must read the Loki event stream"


def test_rendered_queries_target_the_event_stream() -> None:
    for spec in registered_metrics():
        for template in render_targets(spec):
            assert 'service_name="unknown_service"' in template, (
                f"{spec.name} lost the event-stream selector"
            )
            assert "| json" in template, f"{spec.name} lost the | json pipeline"
            assert "| event_name" not in template, (
                f"{spec.name} filters event_name after | json:\n{template}"
            )
            assert "| agent_id" not in template.replace("| {{agent_id}}", ""), (
                f"{spec.name} filters agent_id after | json:\n{template}"
            )


def test_agent_placeholder_renders_per_agent() -> None:
    for spec in registered_metrics():
        rendered = render_query(spec)
        if "{{agent_id}}" in rendered:
            assert "grafana" not in spec.output
            agent_render = render_query(spec, agent_id=123)
            assert "{{agent_id}}" not in agent_render
            assert 'agent_id="123"' in agent_render


def test_dashboard_json_matches_registrations() -> None:
    specs = [s for s in registered_metrics() if "grafana" in s.output]
    path = _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json"
    data = json.loads(path.read_text())
    by_title = {p.get("title"): p for p in data["panels"]}
    for spec in specs:
        panel = by_title[render_title(spec)]
        assert panel["datasource"] == {"type": "loki", "uid": "loki"}
        targets = panel["targets"]
        assert [t["expr"] for t in targets] == render_targets(spec)
        qtype = "instant" if spec.panel == "stat" else "range"
        assert all(t["queryType"] == qtype for t in targets)


def test_plugin_panels_live_under_their_plugin_rows() -> None:
    specs = [s for s in registered_metrics() if "grafana" in s.output]
    path = _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json"
    data = json.loads(path.read_text())
    rows = sorted((p["gridPos"]["y"], p["title"]) for p in data["panels"] if p.get("type") == "row")
    by_title = {p.get("title"): p for p in data["panels"]}
    for spec in specs:
        y = by_title[render_title(spec)]["gridPos"]["y"]
        section = max(row for row in rows if row[0] <= y)[1]
        assert section == spec.plugin, (
            f"{render_title(spec)!r} sits under {section!r}, not its plugin row {spec.plugin!r}"
        )


def _assert_no_dashboard_panel_overlap(panels: list[dict[str, Any]]) -> None:
    for index, panel in enumerate(panels):
        grid = panel["gridPos"]
        for other in panels[index + 1 :]:
            other_grid = other["gridPos"]
            overlaps = (
                grid["x"] < other_grid["x"] + other_grid["w"]
                and other_grid["x"] < grid["x"] + grid["w"]
                and grid["y"] < other_grid["y"] + other_grid["h"]
                and other_grid["y"] < grid["y"] + grid["h"]
            )
            assert not overlaps, (
                f"dashboard gridPos overlap: panel {panel['id']} ({panel['title']!r}) and "
                f"panel {other['id']} ({other['title']!r})"
            )


def _assert_sql_panel_matches_spec(panel: dict[str, Any], expected: list[str]) -> None:
    targets = panel["targets"]
    assert panel["datasource"] == {"type": "postgres", "uid": "ops"}
    assert [target["rawSql"] for target in targets] == expected
    assert all("queryType" not in target for target in targets)


def _assert_promql_panel_matches_spec(
    panel: dict[str, Any], spec: MetricSpec, expected: list[str]
) -> None:
    targets = panel["targets"]
    assert panel["datasource"] == {"type": "prometheus", "uid": "prometheus"}
    assert [target["expr"] for target in targets] == expected
    if spec.panel in {"stat", "table"}:
        assert all(
            target.get("instant") is True and target.get("range") is False for target in targets
        )
    else:
        assert all(target["queryType"] == "range" for target in targets)
    assert spec.target_names is not None
    assert [target["legendFormat"] for target in targets] == spec.target_names


def _assert_loki_panel_matches_spec(
    panel: dict[str, Any], spec: MetricSpec, expected: list[str]
) -> None:
    targets = panel["targets"]
    assert panel["datasource"] == {"type": "loki", "uid": "loki"}
    assert [target["expr"] for target in targets] == expected
    expected_query_type = (
        "instant"
        if spec.query_type == "logql"
        and (spec.panel in {"stat", "table"} or "$__range" in expected[0])
        else "range"
    )
    assert all(target["queryType"] == expected_query_type for target in targets)


def _assert_dashboard_panel_matches_spec(panel: dict[str, Any], spec: MetricSpec) -> None:
    expected = render_targets(spec)
    if spec.query_type == "sql":
        _assert_sql_panel_matches_spec(panel, expected)
    elif spec.query_type == "promql":
        _assert_promql_panel_matches_spec(panel, spec, expected)
    else:
        _assert_loki_panel_matches_spec(panel, spec, expected)


def test_dashboard_json_matches_core_registrations() -> None:
    specs = [spec for spec in _load_core() if "grafana" in spec.output]
    path = _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json"
    data = json.loads(path.read_text())
    panels_by_title = {panel.get("title"): panel for panel in data["panels"]}

    assert len(panels_by_title) == len(data["panels"]), "dashboard panel titles must be unique"
    _assert_no_dashboard_panel_overlap(data["panels"])
    for spec in specs:
        _assert_dashboard_panel_matches_spec(panels_by_title[render_title(spec)], spec)


def test_dashboard_has_97_loki_targets() -> None:
    path = _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json"
    panels = json.loads(path.read_text())["panels"]
    loki_targets = [
        target
        for panel in panels
        for target in panel.get("targets", [])
        if target.get("datasource", panel.get("datasource", {})).get("uid") == "loki"
    ]
    assert len(loki_targets) == 97


def test_unresolved_gauge_names_match_the_otlp_contract() -> None:

    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION, strip_unit_suffix

    specs = {spec.name: spec for spec in _load_core()}
    for field, name in (
        ("unresolved_warnings", "core_unresolved_warning"),
        ("unresolved_errors", "core_unresolved_error"),
        ("dismissed_warnings", "core_dismissed_warning"),
        ("dismissed_errors", "core_dismissed_error"),
    ):
        assert METRIC_DISPOSITION[("resolution_status", field)] == "gauge"
        assert specs[name].query == f"ava_resolution_status_{strip_unit_suffix(field)}_ratio"
        assert specs[name].query_type == "promql"
        assert specs[name].panel == "stat"


def test_agent_max_id_gauge_names_match_the_otlp_contract() -> None:

    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION, strip_unit_suffix

    specs = {spec.name: spec for spec in _load_core()}
    assert METRIC_DISPOSITION[("agent_registry", "max_id")] == "gauge"
    gauge = f"ava_agent_registry_{strip_unit_suffix('max_id')}_ratio"
    assert specs["core_agent_max_id"].query == f"max({gauge})"
    assert specs["core_agent_max_id"].query_type == "promql"
    assert specs["core_agent_max_id"].panel == "timeseries"
    assert specs["core_agent_max_id_growth_rate"].query_type == "promql"
    assert f"deriv(max({gauge})[1h:])" in (specs["core_agent_max_id_growth_rate"].query)


def _pr_flow_daily_query(field: str, unit: str) -> str:
    from base.telemetry.otlp.telemetry_otlp import strip_unit_suffix

    name = f"ava_pr_flow_daily_{strip_unit_suffix(field)}_{unit}"
    return f"max by (day) (last_over_time({name}[26h]))"


def _assert_pr_flow_payload_fields_are_gauges() -> None:
    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION

    for field in (
        "ready_to_merge_median_seconds",
        "ready_to_merge_p90_seconds",
        "flake_new_quarantines",
    ):
        assert METRIC_DISPOSITION[("pr_flow_daily", field)] == "gauge"
    assert METRIC_DISPOSITION[("pr_flow_run", "queue_depth")] == "gauge"


def _assert_pr_flow_specs_match_the_export_job(
    specs: dict[str, MetricSpec],
) -> tuple[MetricSpec, MetricSpec, MetricSpec]:
    from base.telemetry.otlp.telemetry_otlp import strip_unit_suffix

    latency = specs["core_pr_flow_ready_to_merge"]
    assert latency.query_type == "promql"
    assert latency.panel == "table"
    assert latency.unit == "s"
    assert latency.query == _pr_flow_daily_query("ready_to_merge_median_seconds", "seconds")
    assert (latency.targets or []) == [
        _pr_flow_daily_query("ready_to_merge_p90_seconds", "seconds")
    ]
    assert latency.target_names == ["median", "p90"]

    queue = specs["core_pr_flow_queue_depth"]
    assert queue.query_type == "promql"
    assert queue.panel == "timeseries"
    assert queue.query == f"ava_pr_flow_run_{strip_unit_suffix('queue_depth')}_ratio"

    flakes = specs["core_pr_flow_flakes"]
    assert flakes.query == _pr_flow_daily_query("flake_new_quarantines", "ratio")
    assert flakes.target_names == ["new quarantines"]
    return latency, queue, flakes


def _assert_pr_flow_tiles_mirror_specs(
    by_title: dict[Any, dict[str, Any]],
    all_specs: tuple[MetricSpec, MetricSpec, MetricSpec],
    joined_specs: tuple[MetricSpec, MetricSpec],
) -> None:
    for spec in all_specs:
        assert by_title[render_title(spec)]["description"] == spec.description
    for spec in joined_specs:
        joins = [
            t
            for t in by_title[render_title(spec)].get("transformations", [])
            if t["id"] == "joinByField"
        ]
        assert joins, render_title(spec)
        assert joins[0]["options"] == {"byField": "day", "mode": "outer"}


def test_pr_flow_panels_match_the_otlp_contract() -> None:
    specs = {spec.name: spec for spec in _load_core()}
    path = _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json"
    by_title = {panel.get("title"): panel for panel in json.loads(path.read_text())["panels"]}

    _assert_pr_flow_payload_fields_are_gauges()
    latency, queue, flakes = _assert_pr_flow_specs_match_the_export_job(specs)
    _assert_pr_flow_tiles_mirror_specs(by_title, (latency, queue, flakes), (latency, flakes))


def test_dashboard_legends_and_time_ranges_are_explicit() -> None:
    path = _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json"
    panels = json.loads(path.read_text())["panels"]
    for panel in panels:
        for target in panel.get("targets", []):
            datasource = target.get("datasource", panel.get("datasource", {}))
            if datasource.get("uid") == "loki":
                assert target.get("legendFormat"), (panel["id"], target.get("refId"))
        assert not any(
            override.get("matcher", {}).get("id") == "byName"
            and any(property_.get("id") == "displayName" for property_ in override["properties"])
            for override in panel.get("fieldConfig", {}).get("overrides", [])
        ), panel["id"]

    assert all("timeFrom" not in panel for panel in panels)
    assert all("interval" not in panel for panel in panels)


_COST_WINDOW_TILES = (
    "LLM cost (window)",
    "Tokens (window)",
    "LLM cost estimate — day pace",
    "LLM cost estimate — 30-day pace",
    "LLM cost / minute",
)
_COST_DESCRIBED_TILES = (
    "LLM cost estimate — day pace",
    "LLM cost estimate — 30-day pace",
    "LLM cost / minute",
    "LLM cost by model (Top 20)",
    "LLM cost by agent (Top 20)",
)


def _assert_llm_usage_panels_filter_telemetry(panels: list[dict[str, Any]]) -> None:
    for panel in panels:
        expressions = [target.get("expr", "") for target in panel.get("targets", [])]
        if any('event_name="llm_usage"' in expression for expression in expressions):
            assert all('category="telemetry"' in expression for expression in expressions)


def test_cost_dashboard_windows_and_telemetry_contract() -> None:
    path = _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json"
    data = json.loads(path.read_text())
    by_title = {panel.get("title"): panel for panel in data["panels"]}

    assert data["timezone"] == "Asia/Shanghai"
    assert all(panel["collapsed"] is False for panel in data["panels"] if panel["type"] == "row")
    for title in _COST_WINDOW_TILES:
        assert "timeFrom" not in by_title[title]
        assert "interval" not in by_title[title]

    core_by_title = {render_title(spec): spec for spec in _load_core()}
    for title in _COST_DESCRIBED_TILES:
        assert by_title[title]["description"] == core_by_title[title].description

    _assert_llm_usage_panels_filter_telemetry(data["panels"])
