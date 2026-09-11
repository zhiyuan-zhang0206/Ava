"""Registration for plugin inspector widgets (task #2909).

Covers what ``register_inspect_widget`` enforces at import time: PluginContext
attribution, per-plugin id uniqueness, and the closed vocabularies (kind,
target, icon) plus the jumpButtons shape. The per-agent resolution side lives
in `tests/gateway/test_agent_inspect_widgets.py`.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from shared.plugin_context import PluginContext
from shared.plugin_inspector import (
    DuplicateInspectWidget,
    InspectButtonSpec,
    InspectWidgetSpec,
    NoPluginContext,
    clear_registry,
    register_inspect_widget,
    registered_inspect_widgets,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    clear_registry()
    yield
    clear_registry()


def _widget(**over: Any) -> InspectWidgetSpec:
    data: dict[str, Any] = {
        "id": "jump-buttons",
        "kind": "jumpButtons",
        "order": 50,
        "buttons": [InspectButtonSpec(target="notice")],
    }
    data.update(over)
    return InspectWidgetSpec(**data)


def test_register_fills_plugin_and_keeps_order() -> None:
    with PluginContext("ava_fleet"):
        first = register_inspect_widget(_widget(id="one", order=750))
    with PluginContext("other_plugin"):
        second = register_inspect_widget(_widget(id="two", order=10))

    assert first.plugin == "ava_fleet"
    assert second.plugin == "other_plugin"
    assert [w.id for w in registered_inspect_widgets()] == ["one", "two"]
    assert [w.order for w in registered_inspect_widgets()] == [750, 10]


def test_register_replaces_an_author_supplied_plugin() -> None:
    with PluginContext("ava_fleet"):
        spec = register_inspect_widget(_widget(plugin="someone_else"))
    assert spec.plugin == "ava_fleet"


def test_register_outside_plugin_context_refused() -> None:
    with pytest.raises(NoPluginContext):
        register_inspect_widget(_widget())


def test_duplicate_id_within_one_plugin_refused() -> None:
    with PluginContext("ava_fleet"):
        register_inspect_widget(_widget())
        with pytest.raises(DuplicateInspectWidget):
            register_inspect_widget(_widget())


def test_same_id_across_plugins_allowed() -> None:
    with PluginContext("ava_fleet"):
        register_inspect_widget(_widget())
    with PluginContext("other_plugin"):
        register_inspect_widget(_widget())
    assert len(registered_inspect_widgets()) == 2


# ── closed vocabularies ───────────────────────────────────────────────────────


def test_unknown_kind_refused() -> None:
    with pytest.raises(ValidationError):
        _widget(kind="kv")


def test_unknown_target_refused() -> None:
    # model_validate takes Any — the invalid literal would otherwise be a type
    # error at the call site itself.
    with pytest.raises(ValidationError):
        InspectButtonSpec.model_validate({"target": "agent"})


def test_unknown_icon_refused_and_known_icon_accepted() -> None:
    with pytest.raises(ValidationError):
        InspectButtonSpec(target="notice", icon="not-an-icon")
    assert InspectButtonSpec(target="notice", icon="bell").icon == "bell"


def test_jump_buttons_widget_needs_a_button() -> None:
    with pytest.raises(ValidationError):
        _widget(buttons=[])


def test_id_shape_enforced() -> None:
    for bad in ("Bad_ID", "1leading-digit", "has space", ""):
        with pytest.raises(ValidationError):
            _widget(id=bad)


def test_label_and_title_must_be_non_empty_when_present() -> None:
    with pytest.raises(ValidationError):
        InspectButtonSpec(target="notice", label="")
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
