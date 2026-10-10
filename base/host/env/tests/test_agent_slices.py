"""`AgentSlices.resolve`: an agent's pin wins for the fields it holds, every other field is the live
cluster default."""

from __future__ import annotations

import os
from dataclasses import fields
from typing import Any, cast
from unittest.mock import patch

import pytest

from base.config import ConfigBoot, settings
from base.config.agent_pins import resolve_agent_config_pins
from base.host.env import config_registry
from base.host.env.agent_slices import AgentBrain, AgentSlices, ModelOverrides, agent_setting
from base.host.env.config_lite_table import FIELD_DOMAINS

# The agent's pins and the plugin-config view ride the dataclass beside the slices.
_NOT_SLICES = {"pins", "plugin_pins", "_plugin_view", "_default_reader"}


def _default_reader(domain: str, name: str) -> Any:
    return getattr(getattr(settings, domain), name)


def _slice_fields() -> list[str]:
    resolved = AgentSlices.resolve(default_reader=_default_reader)
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
    slices = AgentSlices.resolve(default_reader=_default_reader)
    for name in _slice_fields():
        assert _read_slice(slices, name) == _expected(name, {}), name


def test_explicit_brain_uses_its_owner_without_changing_other_defaults() -> None:
    first = AgentBrain("first-owner-model")
    second = AgentBrain("second-owner-model")
    first_slices = AgentSlices.resolve(brain=first, default_reader=_default_reader)
    second_slices = AgentSlices.resolve(brain=second, default_reader=_default_reader)
    assert first_slices.brain is first
    assert second_slices.brain is second
    for name in _slice_fields():
        if name != "llm_model":
            assert _read_slice(first_slices, name) == _expected(name, {}), name
            assert _read_slice(second_slices, name) == _expected(name, {}), name


def test_every_pinned_field_reads_its_pin() -> None:
    pins = {name: _pinned_value(name) for name in _slice_fields()}
    slices = AgentSlices.resolve(pins, default_reader=_default_reader)
    for name in _slice_fields():
        assert _read_slice(slices, name) == _expected(name, pins), name


@pytest.mark.parametrize("parity", [0, 1])
def test_a_partial_pin_map_pins_some_fields_and_leaves_the_rest_live(parity: int) -> None:
    names = _slice_fields()
    pins = {name: _pinned_value(name) for i, name in enumerate(names) if i % 2 == parity}
    slices = AgentSlices.resolve(pins, default_reader=_default_reader)
    for name in names:
        assert _read_slice(slices, name) == _expected(name, pins), name


def test_the_overlay_wins_over_the_birth_config() -> None:
    pins = resolve_agent_config_pins(
        config_overlay={"llm_model": "overlay-model"},
        birth_config={"llm_model": "birth-model", "memory_inherit_depth": 4},
    )
    slices = AgentSlices.resolve(pins, default_reader=_default_reader)
    assert slices.brain.llm_model == "overlay-model"
    assert slices.memory.memory_inherit_depth == 4


def test_a_live_default_edit_reaches_the_next_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    before = AgentSlices.resolve(default_reader=_default_reader)
    monkeypatch.setattr(
        settings.agent, "history_dump_keep", before.history_dump.history_dump_keep + 3
    )
    after = AgentSlices.resolve(default_reader=_default_reader)
    assert after.history_dump.history_dump_keep == before.history_dump.history_dump_keep + 3
    assert before.history_dump.history_dump_keep != after.history_dump.history_dump_keep


def test_list_settings_are_frozen_into_tuples() -> None:
    slices = AgentSlices.resolve(
        {"sdk_disable": ["ava.x", "ava.y"]}, default_reader=_default_reader
    )
    assert slices.prompt.sdk_disable == ("ava.x", "ava.y")


def test_read_serves_a_setting_no_slice_names_from_the_pin_else_the_live_default() -> None:
    assert AgentSlices.resolve(default_reader=_default_reader).read(
        "sandbox", "exec_timeout_seconds"
    ) == (settings.sandbox.exec_timeout_seconds)
    pinned = AgentSlices.resolve({"exec_timeout_seconds": 123.5}, default_reader=_default_reader)
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


def test_two_owner_defaults_keep_resolved_slices_frozen_and_unsliced_reads_live() -> None:
    with patch.dict(os.environ):
        first, second = ConfigBoot(), ConfigBoot()
        first.set_field("llm_model", "first-model")
        second.set_field("llm_model", "second-model")
        first.set_field("history_dump_keep", 4)
        second.set_field("history_dump_keep", 8)
        first.set_field("exec_timeout_seconds", 101.0)
        second.set_field("exec_timeout_seconds", 202.0)

        def one(domain: str, field: str) -> Any:
            return getattr(getattr(first.view, domain), field)

        def two(domain: str, field: str) -> Any:
            return getattr(getattr(second.view, domain), field)

        a = AgentSlices.resolve(default_reader=one)
        b = AgentSlices.resolve(default_reader=two)
        pinned = AgentSlices.resolve({"exec_timeout_seconds": 303.0}, default_reader=one)
        assert (a.brain.llm_model, b.brain.llm_model) == ("first-model", "second-model")
        assert (a.history_dump.history_dump_keep, b.history_dump.history_dump_keep) == (4, 8)
        first.set_field("history_dump_keep", 12)
        first.set_field("exec_timeout_seconds", 404.0)
        assert a.history_dump.history_dump_keep == 4
        assert AgentSlices.resolve(default_reader=one).history_dump.history_dump_keep == 12
        assert a.read("sandbox", "exec_timeout_seconds") == 404.0
        assert b.read("sandbox", "exec_timeout_seconds") == 202.0
        assert pinned.read("sandbox", "exec_timeout_seconds") == 303.0
        assert agent_setting("exec_timeout_seconds", default_reader=one) == 404.0


def test_fully_pinned_reads_never_call_the_default_reader() -> None:
    def unexpected_default(domain: str, field: str) -> Any:
        raise AssertionError(f"unexpected default read: {domain}.{field}")

    pins = {name: _pinned_value(name) for name in _slice_fields()}
    slices = AgentSlices.resolve(pins, default_reader=unexpected_default)
    assert slices.brain.llm_model == pins["llm_model"]
    assert slices.read("lm", "llm_model") == pins["llm_model"]
    assert agent_setting("llm_model", pins, default_reader=unexpected_default) == pins["llm_model"]
