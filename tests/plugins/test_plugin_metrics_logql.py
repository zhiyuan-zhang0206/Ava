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

from shared.plugin_context import PluginContext
from shared.plugin_metrics import (
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


def test_shipped_plugin_metrics_are_logql() -> None:
    _load_all()
    specs = registered_metrics()
    assert len(specs) == 10
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
