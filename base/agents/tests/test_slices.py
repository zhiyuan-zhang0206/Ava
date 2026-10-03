"""`AgentSlices.resolve` reads every field exactly as the context-bound `turn_settings` view does."""

from __future__ import annotations

from dataclasses import fields
from typing import Any, cast

import pytest

from base.agents.context.slices import AgentSlices
from base.config import settings, turn_settings
from base.config.turn_view import bind_agent_config, resolve_agent_config_pins
from base.host.env import config_registry
from base.host.env.config_lite_table import FIELD_DOMAINS


def _slice_fields() -> list[str]:
    resolved = AgentSlices.resolve()
    return [f.name for group in fields(resolved) for f in fields(getattr(resolved, group.name))]


def _read_slice(slices: AgentSlices, name: str) -> Any:
    for group in fields(slices):
        values = getattr(slices, group.name)
        if name in {f.name for f in fields(values)}:
            return getattr(values, name)
    raise AssertionError(name)


def _view(name: str) -> Any:
    value = getattr(getattr(turn_settings, FIELD_DOMAINS[name]), name)
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
        assert _read_slice(slices, name) == _view(name), name


def test_every_pinned_field_reads_its_pin() -> None:
    pins = {name: _pinned_value(name) for name in _slice_fields()}
    slices = AgentSlices.resolve(pins)
    with bind_agent_config(pins):
        for name in _slice_fields():
            assert _read_slice(slices, name) == _view(name), name


@pytest.mark.parametrize("parity", [0, 1])
def test_a_partial_pin_map_pins_some_fields_and_leaves_the_rest_live(parity: int) -> None:
    names = _slice_fields()
    pins = {name: _pinned_value(name) for i, name in enumerate(names) if i % 2 == parity}
    slices = AgentSlices.resolve(pins)
    with bind_agent_config(pins):
        for name in names:
            assert _read_slice(slices, name) == _view(name), name


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
