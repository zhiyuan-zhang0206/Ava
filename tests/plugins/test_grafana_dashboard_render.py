"""The registry-rendered Ava Ops dashboard — renderer + suppliers (task #3697).

The provisioning file ``deploy/lgtm/config/grafana/provisioning/dashboards/
ava-ops-main.json`` is on its way to becoming a render of the metric
registries instead of a hand-maintained file. This module locks the renderer
(``base.telemetry.metrics.grafana_dashboard``) and the plugin suppliers
(``base.telemetry.metrics.grafana_dashboard_supply``):

1. **Fidelity vs the as-is board** — for every registered ``grafana`` spec,
   the rendered panel must equal its counterpart in the current provisioning
   file, modulo three enumerable normalizations (below). This is the migration
   lock: nothing the user sees may change beyond those.
2. **The layout engine reproduces the file's full geometry** — replaying the
   fixture's 97 entries (sizes + order + the one pinned position + the
   section gaps) through the renderer's flow engine must reproduce every
   gridPos exactly.
3. **Invariants** — unique ids, no overlapping rectangles, the stable
   anchors (uid, timezone, refresh, default range), and byte-determinism
   including a scrubbed-environment re-render.
4. **The dual supplier** — checkout plugins load under their contexts; an
   installed plugin row (registry row + blob) loads through unpack + import.

The three normalizations (each applied to both sides before comparison):

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

All 85 panels are covered since S2 (task #3697) registered the last 18 and
task #3948 added the provider-stall panel, task #2174 the exec-envelope
size/serialize pair; the fixture-only worklist
(``_S2_PENDING``) is empty. The fidelity lock compares panel content AND
geometry (gridPos) panel-for-panel; the layout replay separately locks the
engine on the fixture's full 97-entry geometry.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest

from base.telemetry.metrics.core import catalog
from base.telemetry.metrics.grafana_dashboard import (
    _CORE_SECTIONS_PREFIX,
    _CORE_SECTIONS_SUFFIX,
    DashboardRenderError,
    _Layout,
    render_dashboard,
    render_to_json,
)
from base.telemetry.metrics.grafana_dashboard_supply import (
    collect_plugin_specs,
    load_installed_plugin_specs,
    load_repo_plugin_specs,
)
from base.telemetry.metrics.plugin_metrics import MetricSpec, render_title

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DASHBOARD_FILE = (
    _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json"
)
_PLUGINS = ("ava_fleet", "ava_memory", "ava_syntax_fix")

# Empty since S2 (task #3697) registered the last 18 panels — the Events
# trio, the gateway sample count, and the host + memory-search gauges. Kept as
# the assertion anchor: any fixture panel that stops being covered (a spec
# silently dropping off the board) fails the worklist test below.
_S2_PENDING: set[str] = set()


def _load_world() -> tuple[list[MetricSpec], list[MetricSpec], dict[str, Any]]:
    """Admit the shipped plugin declarations + core metrics and render."""
    plugins = load_repo_plugin_specs()
    assert plugins.failed == []
    core_specs = catalog.collect_core_metrics()
    return core_specs, plugins.specs, render_dashboard(core_specs, plugins.specs)


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


def _canonical_panel(panel: dict[str, Any]) -> dict[str, Any]:
    """Apply the migration normalizations to one panel copy."""
    canonical: dict[str, Any] = cast("dict[str, Any]", json.loads(json.dumps(panel)))
    defaults = canonical.get("fieldConfig", {}).get("defaults")
    if defaults is not None and "thresholds" in defaults:
        thresholds = _canonical_thresholds(defaults["thresholds"])
        if thresholds is None:
            defaults.pop("thresholds", None)
        else:
            defaults["thresholds"] = thresholds
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
    counterpart — the migration lock: content and geometry (gridPos) both,
    modulo the three enumerable normalizations."""
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
        rendered = rendered_by_title.get(render_title(spec))
        assert rendered is not None, f"{spec.name} rendered no panel"
        fixture_panel = fixture_by_title.get(render_title(spec))
        assert fixture_panel is not None, f"{spec.name} has no fixture counterpart"
        expected = _canonical_panel(fixture_panel)
        actual = _canonical_panel(rendered)
        if spec.custom is None:
            # The standard look profile: a fixture panel without a custom block
            # adopts the rendered default (normalization #3). Only a real
            # rendered custom is adopted — a logs panel renders no fieldConfig
            # at all, so no stub is materialized on either side.
            actual_defaults = actual.get("fieldConfig", {}).get("defaults")
            fixture_defaults = expected.get("fieldConfig", {}).get("defaults")
            if (
                actual_defaults is not None
                and actual_defaults.get("custom") is not None
                and (fixture_defaults is None or fixture_defaults.get("custom") is None)
            ):
                expected.setdefault("fieldConfig", {}).setdefault("defaults", {})["custom"] = (
                    actual_defaults["custom"]
                )
        deltas = _panel_diffs(expected, actual, render_title(spec))
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
    """Replay the fixture's 97 entries — sizes and order from the file, the
    pin from the spec, the gaps from the section registry — through the
    renderer's flow engine; every gridPos must come back exactly."""
    core_specs, plugin_specs, _ = world
    fixture = _fixture()
    specs_by_title = {
        render_title(spec): spec
        for spec in [*core_specs, *plugin_specs]
        if "grafana" in spec.output
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
            assert rendered_titles.count(render_title(spec)) == 1, (
                f"{spec.name} must render exactly one panel"
            )

    assert dashboard["uid"] == "ava-ops-main"
    assert dashboard["timezone"] == "Asia/Shanghai"
    assert dashboard["refresh"] == "10m"
    assert dashboard["time"] == {"from": "now-24h", "to": "now"}
    rows = [panel["title"] for panel in panels if panel["type"] == "row"]
    # Every fixture section now has registered specs (S2, task #3697), so the
    # rendered row list is the fixture's row list, in fixture order.
    fixture_rows = [entry["title"] for entry in _fixture()["panels"] if entry["type"] == "row"]
    assert rows == fixture_rows


def test_barchart_panels_keep_the_tick_label_filter_enabled(
    world: tuple[list[MetricSpec], list[MetricSpec], dict[str, Any]],
) -> None:
    """Every barchart panel renders a positive xTickLabelSpacing.

    Spacing 0 disables the chart's tick-label filter: every bar keeps its
    tick label and a dense axis draws the labels on top of each other
    (task #4204). Readability is not machine-testable from the render, so
    the option value is the closest machine-observable proxy."""
    _, _, dashboard = world
    panels = cast("list[dict[str, Any]]", dashboard["panels"])
    charts = [panel for panel in panels if panel["type"] == "barchart"]
    assert charts, "no barchart panels rendered"
    offenders = [
        f"{panel['title']}: xTickLabelSpacing={panel.get('options', {}).get('xTickLabelSpacing')!r}"
        for panel in charts
        if not (
            isinstance(panel.get("options", {}).get("xTickLabelSpacing"), int)
            and panel.get("options", {}).get("xTickLabelSpacing", 0) > 0
        )
    ]
    assert not offenders, "barchart panels with the tick-label filter off:\n" + "\n".join(offenders)


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
        "from base.telemetry.metrics.core import catalog;"
        "from base.telemetry.metrics.grafana_dashboard import render_dashboard, render_to_json;"
        "from base.telemetry.metrics.grafana_dashboard_supply import collect_plugin_specs;"
        "plugins = collect_plugin_specs();"
        "core = catalog.collect_core_metrics();"
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

_LOGQL = 'sum(count_over_time({service_name="unknown_service"} | json [$__range]))'


def _metrics_module_source(name: str, title: str) -> str:
    """A plugin `metrics.py` declaring one LogQL metric called `name`."""
    return (
        "from base.packages.plugins.extensions import PluginContributions\n"
        "from base.telemetry.metrics.plugin_metrics import MetricSpec\n"
        f"METRICS = (MetricSpec(name={name!r}, title={title!r}, event_name='x', "
        f"category='telemetry', query={_LOGQL!r}, query_type='logql', target_names=['a']),)\n"
        "def contribute():\n"
        "    return PluginContributions(metrics=METRICS)\n"
    )


def _install_plugin(conn: psycopg.Connection, tmp_path: Path, name: str, source: str) -> None:
    """Enable an installed plugin row whose blob is a tree holding one `metrics.py`."""
    from base.packages.extensions import registry as registry

    tree = tmp_path / name
    tree.mkdir()
    (tree / "metrics.py").write_text(source)
    digest = registry.put_blob(conn, registry.pack_tree(tree), name=name)
    registry.upsert(conn, name=name, kind="plugin", source="test", content_hash=digest)


def test_repo_supplier_loads_the_shipped_plugins() -> None:
    """The checkout supplier imports each shipped plugin's metrics.py, admits its
    `contribute()` declaration and reports the grafana spec set."""
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
    load — fail-soft, a declaration is admitted whole or not at all."""
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / "good_one").mkdir(parents=True)
    (plugins_dir / "good_one" / "metrics.py").write_text(
        _metrics_module_source("good_one_calls", "Good")
    )
    (plugins_dir / "broken_one").mkdir()
    (plugins_dir / "broken_one" / "metrics.py").write_text("raise RuntimeError('boom')\n")
    import base.telemetry.metrics.grafana_dashboard_supply as supply

    for name in ("good_one", "broken_one"):
        sys.modules.pop(f"ava_repo_plugins.{name}.metrics", None)
    try:
        result = supply.load_repo_plugin_specs(plugins_dir)
    finally:
        for name in ("good_one", "broken_one"):
            sys.modules.pop(f"ava_repo_plugins.{name}.metrics", None)
    assert result.loaded == ["good_one"]
    assert result.failed == ["broken_one"]
    assert [spec.name for spec in result.specs] == ["good_one_calls"]


def test_installed_supplier_loads_a_registry_row(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    """An enabled installed plugin row renders from its blob: register a tree
    with a metrics.py, load it through the supplier, and — after S3's
    ordering — see it in the render."""
    with db_conn.transaction(force_rollback=True):
        _install_plugin(
            db_conn,
            tmp_path,
            "installed_demo",
            _metrics_module_source("installed_demo_calls", "Installed demo"),
        )
        result = load_installed_plugin_specs(db_conn)
    assert result.failed == []
    assert [spec.name for spec in result.specs] == ["installed_demo_calls"]
    assert result.specs[0].plugin == "installed_demo"


def test_collect_refuses_an_installed_plugin_claiming_a_repo_metric_name(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    """Repo and installed declarations share one data registry: a metric name a
    repo plugin already holds is refused for the installed plugin, which is
    reported in `failed` and leaves none of its metrics behind; the repo
    plugin keeps its spec and another installed plugin is unaffected."""
    repo_specs = load_repo_plugin_specs().specs
    taken = repo_specs[0]
    with db_conn.transaction(force_rollback=True):
        _install_plugin(
            db_conn,
            tmp_path,
            "installed_thief",
            _metrics_module_source(taken.name, "Stolen"),
        )
        _install_plugin(
            db_conn,
            tmp_path,
            "installed_fine",
            _metrics_module_source("installed_fine_calls", "Fine"),
        )
        result = collect_plugin_specs(db_conn)
    assert result.failed == ["installed_thief"]
    assert "installed_thief" not in result.loaded
    assert "installed_fine" in result.loaded
    by_name = {spec.name: spec for spec in result.specs}
    assert by_name[taken.name].plugin == taken.plugin
    assert by_name["installed_fine_calls"].plugin == "installed_fine"
    assert {spec.plugin for spec in result.specs} == {*_PLUGINS, "installed_fine"}
