"""`AgentSlices.resolve`: an agent's pin wins for the fields it holds, every other field is the live
cluster default."""

from __future__ import annotations

from dataclasses import fields
from typing import Any, cast

import pytest

from base.config import settings
from base.config.agent_pins import resolve_agent_config_pins
from base.host.env import config_registry
from base.host.env.agent_slices import AgentSlices, ModelOverrides
from base.host.env.config_lite_table import FIELD_DOMAINS

# The agent's pins and the plugin-config view ride the dataclass beside the slices.
_NOT_SLICES = {"pins", "plugin_pins", "_plugin_view"}


def _slice_fields() -> list[str]:
    resolved = AgentSlices.resolve()
    groups = [g for g in fields(resolved) if g.name not in _NOT_SLICES]
    return [f.name for group in groups for f in fields(getattr(resolved, group.name))]


def _read_slice(slices: AgentSlices, name: str) -> Any:
    for group in (g for g in fields(slices) if g.name not in _NOT_SLICES):
        values = getattr(slices, group.name)
        if name in {f.name for f in fields(values)}:
            return getattr(values, name)
    raise AssertionError(name)


def _expected(name: str, pins: dict[str, Any]) -> Any:
    """What the agent holding `pins` reads for `name`: its pin, else the live default."""
    value = pins[name] if name in pins else getattr(getattr(settings, FIELD_DOMAINS[name]), name)
    return tuple(cast("list[Any]", value)) if isinstance(value, list) else value


def _pinned_value(name: str) -> Any:
    current = getattr(getattr(settings, FIELD_DOMAINS[name]), name)
    if isinstance(current, bool):
        return not current
    if isinstance(current, int):
        return current + 7
    if isinstance(current, float):
        return current + 1.5
    if isinstance(current, list):
        return ["pinned", name]
    return f"pinned-{name}"


def test_every_slice_field_is_a_per_agent_setting() -> None:
    fields_by_name = config_registry.fields()
    for name in _slice_fields():
        assert fields_by_name[name].info.json_schema_extra["per_agent"] is True, name  # type: ignore[index]


def test_unpinned_fields_read_the_live_cluster_defaults() -> None:
    slices = AgentSlices.resolve()
    for name in _slice_fields():
        assert _read_slice(slices, name) == _expected(name, {}), name


def test_every_pinned_field_reads_its_pin() -> None:
    pins = {name: _pinned_value(name) for name in _slice_fields()}
    slices = AgentSlices.resolve(pins)
    for name in _slice_fields():
        assert _read_slice(slices, name) == _expected(name, pins), name


@pytest.mark.parametrize("parity", [0, 1])
def test_a_partial_pin_map_pins_some_fields_and_leaves_the_rest_live(parity: int) -> None:
    names = _slice_fields()
    pins = {name: _pinned_value(name) for i, name in enumerate(names) if i % 2 == parity}
    slices = AgentSlices.resolve(pins)
    for name in names:
        assert _read_slice(slices, name) == _expected(name, pins), name


def test_the_overlay_wins_over_the_birth_config() -> None:
    pins = resolve_agent_config_pins(
        config_overlay={"llm_model": "overlay-model"},
        birth_config={"llm_model": "birth-model", "memory_inherit_depth": 4},
    )
    slices = AgentSlices.resolve(pins)
    assert slices.brain.llm_model == "overlay-model"
    assert slices.memory.memory_inherit_depth == 4


def test_a_live_default_edit_reaches_the_next_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    before = AgentSlices.resolve()
    monkeypatch.setattr(
        settings.agent, "history_dump_keep", before.history_dump.history_dump_keep + 3
    )
    after = AgentSlices.resolve()
    assert after.history_dump.history_dump_keep == before.history_dump.history_dump_keep + 3
    assert before.history_dump.history_dump_keep != after.history_dump.history_dump_keep


def test_list_settings_are_frozen_into_tuples() -> None:
    slices = AgentSlices.resolve({"sdk_disable": ["ava.x", "ava.y"]})
    assert slices.prompt.sdk_disable == ("ava.x", "ava.y")


def test_read_serves_a_setting_no_slice_names_from_the_pin_else_the_live_default() -> None:
    assert AgentSlices.resolve().read("sandbox", "exec_timeout_seconds") == (
        settings.sandbox.exec_timeout_seconds
    )
    pinned = AgentSlices.resolve({"exec_timeout_seconds": 123.5})
    assert pinned.read("sandbox", "exec_timeout_seconds") == 123.5


# The completion-notice policy is read by the gateway's notice delivery from the agent's stored
# overlay (`effective_completion_notice_policy`), not inside a turn.
_PER_AGENT_OUTSIDE_THE_SLICES = {"completion_notice_policy"}


def test_every_per_agent_setting_is_in_a_slice_or_read_from_the_stored_overlay() -> None:
    assert config_registry.per_agent_field_names() - set(_slice_fields()) == (
        _PER_AGENT_OUTSIDE_THE_SLICES
    )


def test_overrides_from_pins_reads_only_the_pins() -> None:
    overrides = ModelOverrides.from_pins({"auto_compact_fraction": 0.7, "unrelated": 1})
    assert overrides.auto_compact_fraction == 0.7
    assert overrides.reasoning_effort is None
    assert ModelOverrides.from_pins(None) == ModelOverrides.from_pins({})
