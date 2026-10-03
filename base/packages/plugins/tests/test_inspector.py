"""Declaration of plugin inspector widgets (task #2909; taskList family #3216).

Covers what the data registry enforces when it admits an `inspect_widgets` declaration: plugin
attribution from the registry entry, per-plugin id uniqueness, and the closed kind vocabulary plus
the widget shape. The per-agent resolution side lives in
`ava_builtins/plugins/tests/test_agent_inspect_widgets.py`.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from base.packages.plugins.data_registry import DeclaredFace, build_data_registry
from base.packages.plugins.extensions import PluginContributions
from base.packages.plugins.inspector import InspectWidgetSpec


def _widget(**over: Any) -> InspectWidgetSpec:
    data: dict[str, Any] = {
        "id": "today-tasks",
        "kind": "taskList",
        "order": 150,
    }
    data.update(over)
    return InspectWidgetSpec(**data)


def _face(plugin: str, *widgets: InspectWidgetSpec) -> DeclaredFace:
    return DeclaredFace(plugin, PluginContributions(inspect_widgets=widgets))


def test_admission_fills_plugin_and_keeps_order() -> None:
    registry, refused = build_data_registry(
        [
            _face("ava_fleet", _widget(id="one", order=750)),
            _face("other_plugin", _widget(id="two", order=10)),
        ]
    )

    widgets = list(registry.inspect_widgets())
    assert refused == []
    assert [(w.plugin, w.id) for w in widgets] == [("ava_fleet", "one"), ("other_plugin", "two")]
    assert [w.order for w in widgets] == [750, 10]


def test_admission_replaces_an_author_supplied_plugin() -> None:
    registry, _ = build_data_registry([_face("ava_fleet", _widget(plugin="someone_else"))])
    assert [w.plugin for w in registry.inspect_widgets()] == ["ava_fleet"]


def test_duplicate_id_within_one_plugin_refused() -> None:
    registry, refused = build_data_registry([_face("ava_fleet", _widget(), _widget())])
    assert refused == ["ava_fleet"]
    assert list(registry.inspect_widgets()) == []


def test_same_id_across_plugins_allowed() -> None:
    registry, refused = build_data_registry(
        [_face("ava_fleet", _widget()), _face("other_plugin", _widget())]
    )
    assert refused == []
    assert len(list(registry.inspect_widgets())) == 2


# ── closed vocabularies ───────────────────────────────────────────────────────


def test_unknown_kind_refused() -> None:
    with pytest.raises(ValidationError):
        _widget(kind="kv")


def test_buttons_field_is_gone() -> None:
    # The jumpButtons family was replaced by taskList (#3216); the old field
    # must be refused as unknown rather than accepted and ignored.
    with pytest.raises(ValidationError):
        _widget(buttons=[])


def test_id_shape_enforced() -> None:
    for bad in ("Bad_ID", "1leading-digit", "has space", ""):
        with pytest.raises(ValidationError):
            _widget(id=bad)


def test_title_must_be_non_empty_when_present() -> None:
    with pytest.raises(ValidationError):
        _widget(title="")


def test_unknown_fields_refused() -> None:
    with pytest.raises(ValidationError):
        _widget(resolver="no_such_field")


def test_any_int_order_is_accepted() -> None:
    # The scale is documented, not bounded — a plugin may slot anywhere,
    # including above the notice section (800).
    assert _widget(order=-1).order == -1
    assert _widget(order=10_000).order == 10_000
