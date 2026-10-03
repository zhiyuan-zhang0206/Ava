"""The data registry (metrics + inspector widgets) and the manifest gate on its faces.

`load_declaration` is the fail-soft import of one face; `build_data_registry` admits each
declaration atomically. A failure is reported through `load_report` and costs only that plugin
(or that face).
"""

import importlib
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from base.packages.plugins import data_registry, load_report
from base.packages.plugins.data_registry import (
    METRICS_KEY,
    WIDGETS_KEY,
    DeclaredFace,
    build_data_registry,
    load_declaration,
)
from base.packages.plugins.extensions import PluginContributions
from base.packages.plugins.inspector import InspectWidgetSpec
from base.telemetry.metrics.plugin_metrics import MetricSpec


@pytest.fixture
def reported(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, BaseException]]:
    """Every `report_plugin_load_failure` call, as (plugin, exception)."""
    calls: list[tuple[str, BaseException]] = []

    def record(name: str, exc: BaseException) -> None:
        calls.append((name, exc))

    monkeypatch.setattr(load_report, "report_plugin_load_failure", record)
    return calls


def _metric(name: str = "demo_metric", **over: Any) -> MetricSpec:
    data: dict[str, Any] = {
        "name": name,
        "title": "Demo",
        "event_name": "turn_end",
        "category": "telemetry",
        "query": "SELECT count(*) FROM events WHERE event_name = 'turn_end'",
    }
    data.update(over)
    return MetricSpec(**data)


def _widget(widget_id: str = "demo-widget", **over: Any) -> InspectWidgetSpec:
    data: dict[str, Any] = {"id": widget_id, "kind": "taskList", "order": 150}
    data.update(over)
    return InspectWidgetSpec(**data)


def _face(
    plugin: str,
    metrics: tuple[MetricSpec, ...] = (),
    widgets: tuple[InspectWidgetSpec, ...] = (),
) -> DeclaredFace:
    return DeclaredFace(plugin, PluginContributions(metrics=metrics, inspect_widgets=widgets))


def _module(**attrs: Any) -> ModuleType:
    module = ModuleType("fake_face")
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _importer(module: ModuleType) -> Callable[[], ModuleType]:
    return lambda: module


def _write_manifest(plugin_dir: Path, **contributions: list[str]) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "ava-plugin.json").write_text(
        json.dumps(
            {
                "apiVersion": 2,
                "name": plugin_dir.name,
                "version": "1.0.0",
                "engines": {"ava": ">=0.1.0"},
                "contributions": contributions,
            }
        ),
        encoding="utf-8",
    )


# ── load_declaration: fail-soft ───────────────────────────────────────────────


def test_load_declaration_returns_the_plugins_contributions(
    reported: list[tuple[str, BaseException]],
) -> None:
    contributions = PluginContributions(metrics=(_metric(),))
    face = load_declaration("demo", _importer(_module(contribute=lambda: contributions)))

    assert face == DeclaredFace("demo", contributions)
    assert reported == []


def test_load_declaration_reports_an_importer_that_raises(
    reported: list[tuple[str, BaseException]],
) -> None:
    def boom() -> ModuleType:
        raise ImportError("no such face")

    assert load_declaration("demo", boom) is None
    assert [(name, type(exc)) for name, exc in reported] == [("demo", ImportError)]


def test_load_declaration_reports_a_module_without_contribute(
    reported: list[tuple[str, BaseException]],
) -> None:
    assert load_declaration("demo", _importer(_module())) is None
    assert [(name, type(exc)) for name, exc in reported] == [("demo", AttributeError)]


def test_load_declaration_reports_a_wrong_return_type(
    reported: list[tuple[str, BaseException]],
) -> None:
    assert load_declaration("demo", _importer(_module(contribute=lambda: [_metric()]))) is None
    assert [(name, type(exc)) for name, exc in reported] == [("demo", TypeError)]


def test_load_declaration_reports_a_contribute_that_raises(
    reported: list[tuple[str, BaseException]],
) -> None:
    def contribute() -> PluginContributions:
        raise ValueError("bad spec")

    assert load_declaration("demo", _importer(_module(contribute=contribute))) is None
    assert [(name, type(exc)) for name, exc in reported] == [("demo", ValueError)]


# ── load_declaration: the manifest gate ───────────────────────────────────────


def _gated(plugin_dir: Path, key: str, contributions: PluginContributions) -> DeclaredFace | None:
    return load_declaration(
        plugin_dir.name,
        _importer(_module(contribute=lambda: contributions)),
        gate=(plugin_dir, key),
    )


def test_a_matching_manifest_is_admitted(
    tmp_path: Path, reported: list[tuple[str, BaseException]]
) -> None:
    plugin_dir = tmp_path / "demo"
    _write_manifest(plugin_dir, metrics=["demo_metric"], inspectWidgets=["demo-widget"])

    metrics_face = _gated(plugin_dir, METRICS_KEY, PluginContributions(metrics=(_metric(),)))
    widgets_face = _gated(
        plugin_dir, WIDGETS_KEY, PluginContributions(inspect_widgets=(_widget(),))
    )

    assert metrics_face is not None
    assert widgets_face is not None
    assert reported == []


def test_a_metric_the_manifest_does_not_declare_is_refused(
    tmp_path: Path, reported: list[tuple[str, BaseException]]
) -> None:
    plugin_dir = tmp_path / "demo"
    _write_manifest(plugin_dir, metrics=[])

    assert _gated(plugin_dir, METRICS_KEY, PluginContributions(metrics=(_metric(),))) is None
    assert len(reported) == 1
    assert "'demo_metric' is provided but not declared" in str(reported[0][1])


def test_a_metric_the_declaration_does_not_provide_is_refused(
    tmp_path: Path, reported: list[tuple[str, BaseException]]
) -> None:
    plugin_dir = tmp_path / "demo"
    _write_manifest(plugin_dir, metrics=["demo_metric", "ghost_metric"])

    assert _gated(plugin_dir, METRICS_KEY, PluginContributions(metrics=(_metric(),))) is None
    assert "'ghost_metric' is declared but the plugin does not provide it" in str(reported[0][1])


def test_a_widget_mismatch_is_refused_in_both_directions(
    tmp_path: Path, reported: list[tuple[str, BaseException]]
) -> None:
    undeclared = tmp_path / "undeclared"
    _write_manifest(undeclared, inspectWidgets=[])
    missing = tmp_path / "missing"
    _write_manifest(missing, inspectWidgets=["demo-widget", "ghost-widget"])
    contributions = PluginContributions(inspect_widgets=(_widget(),))

    assert _gated(undeclared, WIDGETS_KEY, contributions) is None
    assert _gated(missing, WIDGETS_KEY, contributions) is None
    assert [name for name, _ in reported] == ["undeclared", "missing"]
    assert "is provided but not declared" in str(reported[0][1])
    assert "is declared but the plugin does not provide it" in str(reported[1][1])


def test_a_plugin_without_a_manifest_is_never_gated(
    tmp_path: Path, reported: list[tuple[str, BaseException]]
) -> None:
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()

    assert _gated(plugin_dir, METRICS_KEY, PluginContributions(metrics=(_metric(),))) is not None
    assert reported == []


def test_a_face_is_gated_only_on_its_own_key(
    tmp_path: Path, reported: list[tuple[str, BaseException]]
) -> None:
    # The manifest declares a widget the metrics face cannot see, and a metric the widgets face
    # cannot see: each face still loads, because it is judged on its own key alone.
    plugin_dir = tmp_path / "demo"
    _write_manifest(plugin_dir, metrics=["demo_metric"], inspectWidgets=["demo-widget"])

    assert _gated(plugin_dir, METRICS_KEY, PluginContributions(metrics=(_metric(),))) is not None
    assert (
        _gated(plugin_dir, WIDGETS_KEY, PluginContributions(inspect_widgets=(_widget(),)))
        is not None
    )
    assert reported == []


# ── build_data_registry ───────────────────────────────────────────────────────


def test_admission_fills_the_plugin_over_what_a_spec_claims(
    reported: list[tuple[str, BaseException]],
) -> None:
    registry, refused = build_data_registry(
        [_face("demo", (_metric(plugin="other"),), (_widget(plugin="other"),))]
    )

    assert refused == []
    assert [(m.name, m.plugin) for m in registry.metrics()] == [("demo_metric", "demo")]
    assert [(w.id, w.plugin) for w in registry.inspect_widgets()] == [("demo-widget", "demo")]
    assert reported == []


def test_a_duplicate_metric_across_plugins_refuses_the_second_and_keeps_the_first(
    reported: list[tuple[str, BaseException]],
) -> None:
    first = _face("first", (_metric("shared"), _metric("first_only")))
    second = _face("second", (_metric("second_only"), _metric("shared")), (_widget(),))

    registry, refused = build_data_registry([first, second])

    assert refused == ["second"]
    assert [(m.name, m.plugin) for m in registry.metrics()] == [
        ("shared", "first"),
        ("first_only", "first"),
    ]
    # Atomic: the refused plugin's other metric and its widget are not admitted either.
    assert list(registry.inspect_widgets()) == []
    assert [name for name, _ in reported] == ["second"]
    assert "'shared'" in str(reported[0][1])


def test_a_duplicate_metric_within_one_plugin_is_refused(
    reported: list[tuple[str, BaseException]],
) -> None:
    registry, refused = build_data_registry([_face("demo", (_metric("dup"), _metric("dup")))])

    assert refused == ["demo"]
    assert list(registry.metrics()) == []
    assert [name for name, _ in reported] == ["demo"]


def test_an_invalid_query_template_refuses_the_plugin(
    reported: list[tuple[str, BaseException]],
) -> None:
    bad = _metric("bad", query="SELECT pg_sleep(1) FROM events")

    registry, refused = build_data_registry(
        [_face("good", (_metric("good"),)), _face("bad_plugin", (_metric("ok"), bad))]
    )

    assert refused == ["bad_plugin"]
    assert [m.name for m in registry.metrics()] == ["good"]
    assert [name for name, _ in reported] == ["bad_plugin"]


def test_a_duplicate_widget_id_refuses_the_plugin(
    reported: list[tuple[str, BaseException]],
) -> None:
    registry, refused = build_data_registry(
        [
            _face("dup", (_metric("dup_metric"),), (_widget("same"), _widget("same"))),
            _face("ok", widgets=(_widget("same"),)),
        ]
    )

    assert refused == ["dup"]
    assert list(registry.metrics()) == []
    assert [(w.plugin, w.id) for w in registry.inspect_widgets()] == [("ok", "same")]
    assert [name for name, _ in reported] == ["dup"]


def test_input_order_is_preserved_and_none_entries_are_skipped(
    reported: list[tuple[str, BaseException]],
) -> None:
    registry, refused = build_data_registry(
        [
            _face("b", (_metric("b_metric"),)),
            None,
            _face("a", (_metric("a_metric"),)),
            None,
        ]
    )

    assert refused == []
    assert [name for name, _ in registry.plugins] == ["b", "a"]
    assert [m.name for m in registry.metrics()] == ["b_metric", "a_metric"]
    assert reported == []


def test_a_refusal_is_reported_through_load_report(
    reported: list[tuple[str, BaseException]],
) -> None:
    assert data_registry.load_report is load_report
    _, refused = build_data_registry([_face("demo", (_metric("x"), _metric("x")))])
    assert refused == ["demo"]
    assert len(reported) == 1


# ── the shipped plugins ───────────────────────────────────────────────────────


@pytest.mark.parametrize("plugin", ["ava_fleet", "ava_memory", "ava_syntax_fix"])
def test_shipped_metrics_faces_declare_admitted_specs(
    plugin: str, reported: list[tuple[str, BaseException]]
) -> None:
    module_name = f"ava_builtins.plugins.{plugin}.metrics"
    face = load_declaration(plugin, lambda: importlib.import_module(module_name))
    assert face is not None
    declared = face.contributions.metrics
    assert declared

    registry, refused = build_data_registry([face])

    assert refused == []
    assert [m.name for m in registry.metrics()] == [m.name for m in declared]
    assert {m.plugin for m in registry.metrics()} == {plugin}
    assert reported == []


def test_shipped_metrics_faces_do_not_collide(
    reported: list[tuple[str, BaseException]],
) -> None:
    faces = [
        load_declaration(
            plugin, lambda p=plugin: importlib.import_module(f"ava_builtins.plugins.{p}.metrics")
        )
        for plugin in ("ava_fleet", "ava_memory", "ava_syntax_fix")
    ]

    _, refused = build_data_registry(faces)

    assert refused == []
    assert reported == []


def test_the_fleet_inspector_face_declares_an_admitted_widget(
    reported: list[tuple[str, BaseException]],
) -> None:
    face = load_declaration(
        "ava_fleet", lambda: importlib.import_module("ava_builtins.plugins.ava_fleet.inspector")
    )
    assert face is not None
    assert face.contributions.inspect_widgets

    registry, refused = build_data_registry([face])

    assert refused == []
    assert [(w.plugin, w.id) for w in registry.inspect_widgets()] == [
        ("ava_fleet", w.id) for w in face.contributions.inspect_widgets
    ]
    assert reported == []
