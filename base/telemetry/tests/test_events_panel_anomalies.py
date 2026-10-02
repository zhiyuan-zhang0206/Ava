"""The Events panel lists the declared anomaly names, and the shipped dashboard matches."""

from __future__ import annotations

import json
from pathlib import Path

from base.telemetry.metrics.core import catalog

_DASHBOARD = (
    Path(__file__).resolve().parents[3]
    / "deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json"
)


def _what_happened_query() -> str:
    [spec] = [s for s in catalog.collect_core_metrics() if s.name == "core_events_what_happened"]
    return spec.query


def test_the_exec_memory_guard_kill_is_a_listed_anomaly() -> None:
    assert "|exec_memory_guard_killed|" in _what_happened_query()


def test_the_shipped_dashboard_lists_it_in_the_same_panel() -> None:
    dashboard = json.loads(_DASHBOARD.read_text(encoding="utf-8"))
    expressions = [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if "expr" in target
    ]
    assert any("|exec_memory_guard_killed|" in expr for expr in expressions)
