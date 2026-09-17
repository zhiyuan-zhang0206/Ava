"""The registry-rendered Ava Ops dashboard — renderer + suppliers (task #3697 S1).

The provisioning file ``deploy/lgtm/config/grafana/provisioning/dashboards/
ava-ops-main.json`` is on its way to becoming a render of the metric
registries instead of a hand-maintained file. This module locks the renderer
(``shared.grafana_dashboard``) and the plugin suppliers
(``shared.grafana_dashboard_supply``):

1. **Fidelity vs the as-is board** — for every registered ``grafana`` spec,
   the rendered panel must equal its counterpart in the current provisioning
   file, modulo four enumerable normalizations (below). This is the migration
   lock: nothing the user sees may change beyond those.
2. **The layout engine reproduces the file's full geometry** — replaying the
   fixture's 94 entries (sizes + order + the one pinned position + the
   section gaps) through the renderer's flow engine must reproduce every
   gridPos exactly.
3. **Invariants** — unique ids, no overlapping rectangles, the stable
   anchors (uid, timezone, refresh, default range), and byte-determinism
   including a scrubbed-environment re-render.
4. **The dual supplier** — checkout plugins load under their contexts; an
   installed plugin row (registry row + blob) loads through unpack + import.

The four normalizations (each applied to both sides before comparison):

- **plugin block ids** shift by −1 for ids >= 1007 in the 1000-block: the
  historical file left a gap where a deleted panel used to be; the renderer
  allocates plugin ids densely (task #3697 decision).
- **threshold "no rules" encodings** — a missing key, ``null``, an empty step
  list, and a bare green base all mean "no threshold rules" and compare
  equal.
- **refIds are positional** — the engine labels targets A, B, C, ... by
  order; the fixture's gateway-latency panel carries its hand-edited A, B, D,
  C, which is internal bookkeeping (no panel references a refId).
- **the standard ts/barchart look profile** — a fixture panel without a
  ``custom`` block compares equal to the rendered default profile when the
  spec sets no custom (the compaction pair and the cost barchart lacked it;
  the render gives every chart the one standard look).

What this file does NOT lock yet: full-set geometric equality — the fixture
also contains 18 panels that are still hand-written (the S2 registration
worklist, pinned as ``_S2_PENDING`` below); until they render, sections have
different y offsets. When S2 lands, the fidelity test grows to all 84 panels
and ``_S2_PENDING`` empties.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest

from shared import core_metrics
from shared.grafana_dashboard import (
    _CORE_SECTIONS_PREFIX,
    _CORE_SECTIONS_SUFFIX,
    DashboardRenderError,
    _Layout,
    render_dashboard,
    render_to_json,
)
from shared.grafana_dashboard_supply import (
    load_installed_plugin_specs,
    load_repo_plugin_specs,
)
from shared.plugin_context import PluginContext
from shared.plugin_metrics import MetricSpec, clear_registry, registered_metrics

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DASHBOARD_FILE = (
    _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json"
)
_PLUGINS = ("ava_code", "ava_fleet", "ava_memory")

# The fixture panels still hand-written (the S2 migration worklist, task
# #3697): they have no specs yet, so the render cannot cover them.
_S2_PENDING = {
    "Events — What happened (T0+T1)",
    "Events — types",
    "Events — raw stream (all, incl. noise)",
    "Gateway latency sample count by route",
    "CPU utilization",
    "Memory used",
    "Load average",
    "Filesystem used",
    "Disk throughput",
    "Network throughput",
    "Postgres connections",
    "Postgres transactions",
    "Database size",
    "Redis memory",
    "Redis clients and evictions",
    "Redis throughput",
    "Memory search rows",
    "Memory search npz save duration",
}


def _load_world() -> tuple[list[MetricSpec], list[MetricSpec], dict[str, Any]]:
    """Register the shipped plugin + core metrics from fresh modules and
    render — the registry-hygiene pattern the existing sync-lock test uses."""
    clear_registry()
    core_metrics.clear_core_registry()
    for name in _PLUGINS:
        module_name = f"ava_builtins.plugins.{name}.metrics"
        module = sys.modules.get(module_name)
        with PluginContext(name):
            if module is None:
                importlib.import_module(module_name)
            else:
                importlib.reload(module)
    for module_name in core_metrics._CORE_DEFINITION_MODULES:
        sys.modules.pop(module_name, None)
    core_specs = core_metrics.collect_core_metrics()
    plugin_specs = registered_metrics()
    return core_specs, plugin_specs, render_dashboard(core_specs, plugin_specs)


@pytest.fixture(scope="module")
def world() -> tuple[list[MetricSpec], list[MetricSpec], dict[str, Any]]:
    return _load_world()


def _fixture() -> dict[str, Any]:
    return json.loads(_DASHBOARD_FILE.read_text(encoding="utf-8"))


# ── fidelity normalizations ───────────────────────────────────────────────────


def _canonical_thresholds(value: Any) -> dict[str, Any] | None:
    """No-rules encodings collapse to None; rule-bearing steps keep the green
    base plus the colored steps."""
    if not isinstance(value, dict):
        return None
    mapping = cast("dict[str, Any]", value)
    steps: list[Any] = mapping.get("steps") or []
    colored = [step for step in steps if step.get("color") != "green"]
    if not colored:
        return None
    return {"mode": "absolute", "steps": [{"color": "green", "value": None}, *colored]}


def _canonical_panel(panel: dict[str, Any], *, from_fixture: bool) -> dict[str, Any]:
    """Apply the migration normalizations to one panel copy."""
    canonical: dict[str, Any] = cast("dict[str, Any]", json.loads(json.dumps(panel)))
    defaults = canonical.get("fieldConfig", {}).get("defaults")
    if defaults is not None and "thresholds" in defaults:
        thresholds = _canonical_thresholds(defaults["thresholds"])
        if thresholds is None:
            defaults.pop("thresholds", None)
        else:
            defaults["thresholds"] = thresholds
    panel_id = canonical.get("id")
    if from_fixture and panel_id is not None and 1007 <= panel_id < 2000:
        canonical["id"] = panel_id - 1
    for index, target in enumerate(canonical.get("targets", [])):
        if "refId" in target:
            target["refId"] = chr(ord("A") + index)
    return canonical


def _panel_diffs(a: Any, b: Any, path: str) -> list[str]:
    """Structural JSON diff — the list of "path: value != value" strings."""
    out: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        mapping_a = cast("dict[str, Any]", a)
        mapping_b = cast("dict[str, Any]", b)
        for key in sorted(set(mapping_a) | set(mapping_b)):
            if key not in mapping_a:
                out.append(f"{path}.{key}: missing in fixture, render={mapping_b[key]!r}")
            elif key not in mapping_b:
                out.append(f"{path}.{key}: fixture={mapping_a[key]!r}, missing in render")
            else:
                out.extend(_panel_diffs(mapping_a[key], mapping_b[key], f"{path}.{key}"))
    elif isinstance(a, list) and isinstance(b, list):
        list_a = cast("list[Any]", a)
        list_b = cast("list[Any]", b)
        if len(list_a) != len(list_b):
            out.append(f"{path}: len {len(list_a)} != {len(list_b)}")
        else:
            for index, (item_a, item_b) in enumerate(zip(list_a, list_b, strict=True)):
                out.extend(_panel_diffs(item_a, item_b, f"{path}[{index}]"))
    elif a != b:
        out.append(f"{path}: fixture={a!r} != render={b!r}")
    return out


# ── fidelity ──────────────────────────────────────────────────────────────────


def test_rendered_panels_match_the_provisioning_file(
    world: tuple[list[MetricSpec], list[MetricSpec], dict[str, Any]],
) -> None:
    """Every registered grafana spec renders one panel equal to its fixture
    counterpart — the migration lock (gridPos excluded until S2 lands; the
    engine's geometry is locked separately below)."""
    core_specs, plugin_specs, dashboard = world
    fixture = _fixture()
    fixture_by_title: dict[str, Any] = {
        panel["title"]: panel for panel in fixture["panels"] if panel["type"] != "row"
    }
    rendered_by_title: dict[str, Any] = {
        panel["title"]: panel for panel in dashboard["panels"] if panel["type"] != "row"
    }

    problems: list[str] = []
    for spec in [*core_specs, *plugin_specs]:
        if "grafana" not in spec.output:
            continue
        rendered = rendered_by_title.get(spec.title)
        assert rendered is not None, f"{spec.name} rendered no panel"
        fixture_panel = fixture_by_title.get(spec.title)
        assert fixture_panel is not None, f"{spec.name} has no fixture counterpart"
        expected = _canonical_panel(fixture_panel, from_fixture=True)
        actual = _canonical_panel(rendered, from_fixture=False)
        if spec.custom is None:
            # The standard look profile: a fixture panel without a custom block
            # adopts the rendered default (normalization #4).
            expected_defaults = expected.setdefault("fieldConfig", {}).setdefault("defaults", {})
            if expected_defaults.get("custom") is None:
                actual_defaults = actual.get("fieldConfig", {}).get("defaults", {})
                if actual_defaults.get("custom") is not None:
                    expected_defaults["custom"] = actual_defaults["custom"]
        for key in ("gridPos",):
            expected.pop(key, None)
            actual.pop(key, None)
        deltas = _panel_diffs(expected, actual, spec.title)
        if deltas:
            problems.append(f"{spec.name}: " + "; ".join(deltas[:6]))
    assert not problems, "rendered panels diverged from the provisioning file:\n" + "\n".join(
        problems
    )


def test_fixture_only_panels_are_exactly_the_s2_worklist(
    world: tuple[list[MetricSpec], list[MetricSpec], dict[str, Any]],
) -> None:
    """Nothing silently drops off the board: the fixture panels without a spec
    are exactly the pinned S2 registration worklist."""
    _, _, dashboard = world
    fixture = _fixture()
    rendered_titles = {panel["title"] for panel in dashboard["panels"] if panel["type"] != "row"}
    uncovered = {
        panel["title"] for panel in fixture["panels"] if panel["type"] != "row"
    } - rendered_titles
    assert uncovered == _S2_PENDING


def test_rendered_sequence_follows_the_fixture(
    world: tuple[list[MetricSpec], list[MetricSpec], dict[str, Any]],
) -> None:
    """The render's order (rows + covered panels) equals the fixture's order
    restricted to the covered entries — the ``order`` field's lock."""
    _, _, dashboard = world
    fixture = _fixture()
    rendered_titles = {panel["title"] for panel in dashboard["panels"] if panel["type"] != "row"}
    rendered_rows = {panel["title"] for panel in dashboard["panels"] if panel["type"] == "row"}
    fixture_sequence = [
        ("row" if panel["type"] == "row" else "panel", panel["title"])
        for panel in fixture["panels"]
        if (panel["type"] == "row" and panel["title"] in rendered_rows)
        or (panel["type"] != "row" and panel["title"] in rendered_titles)
    ]
    rendered_sequence = [
        ("row" if panel["type"] == "row" else "panel", panel["title"])
        for panel in dashboard["panels"]
    ]
    assert fixture_sequence == rendered_sequence


# ── the layout engine ─────────────────────────────────────────────────────────


def test_layout_engine_reproduces_the_full_fixture_geometry(
    world: tuple[list[MetricSpec], list[MetricSpec], dict[str, Any]],
) -> None:
    """Replay the fixture's 94 entries — sizes and order from the file, the
    pin from the spec, the gaps from the section registry — through the
    renderer's flow engine; every gridPos must come back exactly."""
    core_specs, plugin_specs, _ = world
    fixture = _fixture()
    specs_by_title = {
        spec.title: spec for spec in [*core_specs, *plugin_specs] if "grafana" in spec.output
    }
    sections = {section.title: section for section in _CORE_SECTIONS_PREFIX + _CORE_SECTIONS_SUFFIX}

    layout = _Layout()
    mismatches: list[str] = []
    for entry in fixture["panels"]:
        grid = entry["gridPos"]
        if entry["type"] == "row":
            section = sections.get(entry["title"])
            layout.section(gap_before=section.gap_before if section else 0)
            placed = layout.place(24, 1)
        else:
            spec = specs_by_title.get(entry["title"])
            pin = spec.position if spec is not None else None
            placed = layout.place(grid["w"], grid["h"], position=pin)
        expected = {"h": grid["h"], "w": grid["w"], "x": grid["x"], "y": grid["y"]}
        if placed != expected:
            mismatches.append(
                f"entry {entry['id']} ({entry.get('title')!r}): {placed} != {expected}"
            )
    assert not mismatches, "\n".join(mismatches)


# ── invariants ────────────────────────────────────────────────────────────────


def test_render_invariants(
    world: tuple[list[MetricSpec], list[MetricSpec], dict[str, Any]],
) -> None:
    """Unique ids, no overlapping rectangles, one panel per grafana spec, and
    the stable shell anchors."""
    core_specs, plugin_specs, dashboard = world
    panels = cast("list[dict[str, Any]]", dashboard["panels"])

    ids = [panel["id"] for panel in panels]
    assert len(ids) == len(set(ids)), "panel ids must be unique"

    for index, panel in enumerate(panels):
        grid = panel["gridPos"]
        for other in panels[index + 1 :]:
            other_grid = other["gridPos"]
            overlap = (
                grid["x"] < other_grid["x"] + other_grid["w"]
                and other_grid["x"] < grid["x"] + grid["w"]
                and grid["y"] < other_grid["y"] + other_grid["h"]
                and other_grid["y"] < grid["y"] + grid["h"]
            )
            assert not overlap, f"gridPos overlap: {panel['id']} vs {other['id']}"

    rendered_titles = [panel["title"] for panel in panels if panel["type"] != "row"]
    for spec in [*core_specs, *plugin_specs]:
        if "grafana" in spec.output:
            assert rendered_titles.count(spec.title) == 1, (
                f"{spec.name} must render exactly one panel"
            )

    assert dashboard["uid"] == "ava-ops-main"
    assert dashboard["timezone"] == "Asia/Shanghai"
    assert dashboard["refresh"] == "10m"
    assert dashboard["time"] == {"from": "now-24h", "to": "now"}
    rows = [panel["title"] for panel in panels if panel["type"] == "row"]
    # A section renders its row iff it has panels: every fixture row that
    # renders keeps its fixture order, and the Host & data plane row is absent
    # until its S2-pending specs register.
    fixture_rows = [entry["title"] for entry in _fixture()["panels"] if entry["type"] == "row"]
    assert rows == [title for title in fixture_rows if title in set(rows)]
    assert "Host & data plane" not in rows  # all its panels are S2-pending (_S2_PENDING)


def test_render_rejects_unplaced_core_specs() -> None:
    """A core spec without its placement pins fails loudly rather than
    rendering into an arbitrary section."""
    spec = MetricSpec(
        name="test_unplaced",
        title="Unplaced",
        event_name="llm_usage",
        category="telemetry",
        query='sum(count_over_time({service_name="unknown_service"} | json [$__range]))',
        query_type="logql",
        target_names=["x"],
    )
    with pytest.raises(DashboardRenderError, match="no section"):
        render_dashboard([spec], [])


def test_render_is_deterministic_and_environment_independent(
    world: tuple[list[MetricSpec], list[MetricSpec], dict[str, Any]],
) -> None:
    """Same specs, same bytes — twice in-process, and once more in a
    subprocess with a scrubbed environment (the #3339/#2456 lesson)."""
    core_specs, plugin_specs, dashboard = world
    rendered = render_to_json(dashboard)
    assert rendered == render_to_json(render_dashboard(core_specs, plugin_specs))

    script = (
        "import hashlib, sys;"
        f"sys.path.insert(0, {str(_REPO_ROOT)!r});"
        "from shared import core_metrics;"
        "from shared.grafana_dashboard import render_dashboard, render_to_json;"
        "from shared.grafana_dashboard_supply import collect_plugin_specs;"
        "plugins = collect_plugin_specs();"
        "core = core_metrics.collect_core_metrics();"
        "print(hashlib.sha256(render_to_json(render_dashboard(core, plugins.specs)).encode()).hexdigest())"
    )
    scrubbed = subprocess.run(  # noqa: S603 — our own interpreter + a literal script
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd="/",
        env={"PATH": "/usr/bin:/bin"},
        check=True,
    )
    import hashlib

    assert scrubbed.stdout.strip() == hashlib.sha256(rendered.encode()).hexdigest()


# ── plugin suppliers ──────────────────────────────────────────────────────────


def test_repo_supplier_loads_the_shipped_plugins() -> None:
    """The checkout supplier imports each shipped plugin's metrics.py under
    its plugin context and reports the grafana spec set."""
    clear_registry()
    for name in _PLUGINS:
        sys.modules.pop(f"ava_builtins.plugins.{name}.metrics", None)
    result = load_repo_plugin_specs()
    assert result.loaded == list(_PLUGINS)
    assert result.failed == []
    grafana = [spec for spec in result.specs if "grafana" in spec.output]
    assert len(grafana) == 9
    assert {spec.plugin for spec in result.specs} == set(_PLUGINS)


def test_repo_supplier_reports_a_broken_plugin_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A metrics.py that raises is skipped with a report; the rest still
    load — fail-soft, never a half-imported plugin left registered."""
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / "good_one").mkdir(parents=True)
    (plugins_dir / "good_one" / "metrics.py").write_text(
        "from shared.plugin_metrics import MetricSpec, register_metric\n"
        "register_metric(MetricSpec(name='good_one_calls', title='Good', event_name='x', "
        "category='telemetry', query='sum(count_over_time({service_name=\"unknown_service\"} | "
        "json [$__range]))', query_type='logql', target_names=['a']))\n"
    )
    (plugins_dir / "broken_one").mkdir()
    (plugins_dir / "broken_one" / "metrics.py").write_text("raise RuntimeError('boom')\n")
    import shared.grafana_dashboard_supply as supply

    monkeypatch.setattr(supply, "_REPO_PLUGINS_DIR", plugins_dir)
    for name in ("good_one", "broken_one"):
        sys.modules.pop(f"ava_repo_plugins.{name}.metrics", None)
    clear_registry()
    result = supply.load_repo_plugin_specs()
    assert result.loaded == ["good_one"]
    assert result.failed == ["broken_one"]
    assert [spec.name for spec in result.specs] == ["good_one_calls"]


def test_installed_supplier_loads_a_registry_row(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    """An enabled installed plugin row renders from its blob: register a tree
    with a metrics.py, load it through the supplier, and — after S3's
    ordering — see it in the render."""
    from shared import extension_registry as registry

    tree = tmp_path / "installed_plugin"
    tree.mkdir()
    (tree / "metrics.py").write_text(
        "from shared.plugin_metrics import MetricSpec, register_metric\n"
        "register_metric(MetricSpec(name='installed_demo_calls', title='Installed demo', "
        "event_name='x', category='telemetry', query='sum(count_over_time("
        "{service_name=\"unknown_service\"} | json [$__range]))', query_type='logql', "
        "target_names=['a']))\n"
    )
    with db_conn.transaction(force_rollback=True):
        digest = registry.put_blob(db_conn, registry.pack_tree(tree), name="installed_demo")
        registry.upsert(
            db_conn,
            name="installed_demo",
            kind="plugin",
            source="test",
            content_hash=digest,
        )
        result = load_installed_plugin_specs(db_conn)
    assert result.failed == []
    assert [spec.name for spec in result.specs] == ["installed_demo_calls"]
    assert result.specs[0].plugin == "installed_demo"
