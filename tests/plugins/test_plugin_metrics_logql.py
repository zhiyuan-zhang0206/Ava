"""Lock the plugin-metric LogQL cutover (task #180): every shipped plugin
metric reads the live Loki event stream, not the frozen PG `events` table.

The dashboard JSON (`deploy/lgtm/config/grafana/provisioning/dashboards/
ava-ops-main.json`) is hand-maintained since the generator did not survive the
archive->public port — these tests lock the registered specs the JSON mirrors:
a spec regressing to SQL, rendering without the event-stream selector, or a
JSON panel drifting from its spec fails here.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from shared import core_metrics
from shared.plugin_context import PluginContext
from shared.plugin_metrics import (
    MetricSpec,
    clear_registry,
    registered_metrics,
    render_query,
    render_targets,
)

_PLUGINS = ("ava_code", "ava_fleet", "ava_memory")
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_all() -> None:
    """Register every shipped plugin metric exactly once, reloading modules
    that an earlier test already imported so the registry state is exact
    regardless of what ran before."""
    clear_registry()
    for name in _PLUGINS:
        mod_name = f"ava_builtins.plugins.{name}.metrics"
        mod = sys.modules.get(mod_name)
        with PluginContext(name):
            if mod is None:
                importlib.import_module(mod_name)
            else:
                importlib.reload(mod)


def _load_core() -> list[MetricSpec]:
    """Register the complete core metric set from fresh definition modules."""
    core_metrics.clear_core_registry()
    for module_name in (
        "shared.core_metrics_panels",
        "shared.core_metrics_observability",
    ):
        sys.modules.pop(module_name, None)
    return core_metrics.collect_core_metrics()


def test_shipped_plugin_metrics_are_logql() -> None:
    _load_all()
    specs = registered_metrics()
    assert len(specs) == 9
    for spec in specs:
        assert spec.query_type == "logql", (
            f"{spec.name} must read Loki, not the frozen events table"
        )


def test_rendered_queries_target_the_event_stream() -> None:
    _load_all()
    for spec in registered_metrics():
        for template in render_targets(spec):
            assert '{service_name="unknown_service"}' in template, (
                f"{spec.name} lost the event-stream selector"
            )
            assert "| json" in template, f"{spec.name} lost the | json pipeline"
            assert "FROM events" not in template, f"{spec.name} still reads the frozen events table"


def test_agent_placeholder_renders_per_agent() -> None:
    _load_all()
    for spec in registered_metrics():
        rendered = render_query(spec)
        if "{{agent_id}}" in rendered:
            assert "grafana" not in spec.output
            agent_render = render_query(spec, agent_id=123)
            assert "{{agent_id}}" not in agent_render
            assert 'agent_id="123"' in agent_render


def test_dashboard_json_matches_registrations() -> None:
    """The merged dashboard mirrors the registered grafana specs panel for
    panel: same Loki datasource, the rendered expr verbatim, instant queries
    for stat panels and range queries for the rest."""
    _load_all()
    specs = [s for s in registered_metrics() if "grafana" in s.output]
    path = _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json"
    data = json.loads(path.read_text())
    by_title = {p.get("title"): p for p in data["panels"]}
    for spec in specs:
        panel = by_title[spec.title]
        assert panel["datasource"] == {"type": "loki", "uid": "loki"}
        targets = panel["targets"]
        assert [t["expr"] for t in targets] == render_targets(spec)
        qtype = "instant" if spec.panel == "stat" else "range"
        assert all(t["queryType"] == qtype for t in targets)


def test_dashboard_json_matches_core_registrations() -> None:
    """Every core Grafana spec has one exact dashboard counterpart.

    The dashboard is hand-maintained, so this protects the user-visible
    queries, datasource, and Loki query mode against either registry or JSON
    drifting independently. A ``$__range`` aggregate is a window total and
    must be instant; fixed-width bucket queries must be range queries.
    """
    specs = [spec for spec in _load_core() if "grafana" in spec.output]
    path = _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json"
    data = json.loads(path.read_text())
    panels_by_title = {panel.get("title"): panel for panel in data["panels"]}

    assert len(panels_by_title) == len(data["panels"]), "dashboard panel titles must be unique"
    for spec in specs:
        panel = panels_by_title[spec.title]
        targets = panel["targets"]
        expected = render_targets(spec)
        if spec.query_type == "sql":
            assert panel["datasource"] == {"type": "postgres", "uid": "ops"}
            assert [target["rawSql"] for target in targets] == expected
            assert all("queryType" not in target for target in targets)
            continue

        assert panel["datasource"] == {"type": "loki", "uid": "loki"}
        assert [target["expr"] for target in targets] == expected
        expected_query_type = (
            "instant" if spec.panel in {"stat", "table"} or "$__range" in expected[0] else "range"
        )
        assert all(target["queryType"] == expected_query_type for target in targets)


def test_dashboard_legends_and_24h_stats_are_explicit() -> None:
    """Loki names and the six Statistics windows are dashboard contracts."""
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

    panels_by_id = {panel["id"]: panel for panel in panels}
    for panel_id in (7, 8, 44, 45, 46, 47):
        assert panels_by_id[panel_id]["timeFrom"] == "now-24h"


def test_cost_dashboard_windows_and_telemetry_contract() -> None:
    """Cost tiles from #384 use their named windows; no llm_usage panel reads logs."""
    path = _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json"
    data = json.loads(path.read_text())
    by_title = {panel.get("title"): panel for panel in data["panels"]}

    assert data["timezone"] == "Asia/Shanghai"
    assert all(panel["collapsed"] is False for panel in data["panels"] if panel["type"] == "row")
    assert by_title["LLM cost (24h)"]["timeFrom"] == "now-24h"
    assert by_title["Tokens (24h)"]["timeFrom"] == "now-24h"
    assert by_title["Today LLM cost estimate"]["timeFrom"] == "now/d"
    assert by_title["This-month LLM cost estimate"]["timeFrom"] == "now/M"
    assert by_title["Daily LLM cost (7d)"]["timeFrom"] == "now-7d/d"
    assert by_title["Daily LLM cost (7d)"]["interval"] == "24h"

    core_by_title = {spec.title: spec for spec in _load_core()}
    for title in (
        "Today LLM cost estimate",
        "This-month LLM cost estimate",
        "Next-month LLM cost estimate",
        "Daily LLM cost (7d)",
        "LLM cost by model (Top 20)",
        "LLM cost by agent (Top 20)",
    ):
        assert by_title[title]["description"] == core_by_title[title].description

    for panel in data["panels"]:
        expressions = [target.get("expr", "") for target in panel.get("targets", [])]
        if any('event_name="llm_usage"' in expression for expression in expressions):
            assert all('category="telemetry"' in expression for expression in expressions)
