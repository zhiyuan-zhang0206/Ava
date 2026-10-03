"""Contract tests for plugin declarations and reads of core configuration flags."""

from collections.abc import Callable, Iterator

import pytest

from base.config import get_field, set_field
from base.host.env.agent_slices import AgentSlices
from base.host.env.config_registry import fields
from base.lm.registry import DEFAULT_TUNING
from base.packages.plugins.config_registration import _field_is_sensitive
from base.packages.plugins.flags import (
    UndeclaredFlag,
    UnknownFlag,
    declare_flags,
    declared_flags,
    read_flag,
)

FLAG = "agent.prompt_invest_future_enabled"


@pytest.fixture
def declare() -> Iterator[Callable[[str, tuple[str, ...]], Callable[[], None]]]:
    """`declare_flags` whose every declaration is undone at teardown (the registry is module state)."""
    undos: list[Callable[[], None]] = []

    def _declare(plugin: str, keys: tuple[str, ...]) -> Callable[[], None]:
        undo = declare_flags(plugin, keys)
        undos.append(undo)
        return undo

    yield _declare
    for undo in reversed(undos):
        undo()


@pytest.mark.parametrize(
    "key",
    ["nodot", "a.b.c", "", "bogus.x", "agent.bogus_field", "data_plane.db_url"],
)
def test_declare_flags_rejects_invalid_or_sensitive_keys(key: str) -> None:
    if key == "data_plane.db_url":
        assert _field_is_sensitive(fields()["db_url"].info.json_schema_extra)
    with pytest.raises(UnknownFlag) as exc_info:
        declare_flags("plugin", (key,))
    assert repr(key) in str(exc_info.value)
    assert declared_flags("plugin") == frozenset()


def test_declare_flags_validates_every_key_before_recording_any() -> None:
    with pytest.raises(UnknownFlag):
        declare_flags("plugin", (FLAG, "bogus.x"))
    assert declared_flags("plugin") == frozenset()


def test_declare_flags_registers_a_valid_key(declare) -> None:
    declare("plugin", (FLAG,))

    assert declared_flags("plugin") == {FLAG}


def test_declarations_are_per_plugin_and_shared_keys_read_alike(declare) -> None:
    declare("first", (FLAG,))
    declare("second", (FLAG,))

    assert declared_flags("first") == {FLAG}
    assert declared_flags("second") == {FLAG}
    first_value = read_flag(FLAG, AgentSlices.resolve(), plugin="first")
    assert read_flag(FLAG, AgentSlices.resolve(), plugin="second") is first_value


def test_read_flag_requires_a_plugin_name() -> None:
    with pytest.raises(TypeError):
        read_flag(FLAG, AgentSlices.resolve())  # type: ignore[call-arg]


def test_read_flag_reads_the_named_plugins_declaration(declare) -> None:
    previous = get_field("prompt_invest_future_enabled")
    try:
        set_field("prompt_invest_future_enabled", False)
        declare("plugin", (FLAG,))

        assert read_flag(FLAG, AgentSlices.resolve(), plugin="plugin") is False
    finally:
        set_field("prompt_invest_future_enabled", previous)


def test_read_flag_rejects_a_plugin_that_did_not_declare_the_key(declare) -> None:
    declare("declared-plugin", (FLAG,))

    assert read_flag(FLAG, AgentSlices.resolve(), plugin="declared-plugin") is True
    with pytest.raises(UndeclaredFlag, match="declaration is contract"):
        read_flag(FLAG, AgentSlices.resolve(), plugin="other-plugin")


def test_read_flag_requires_a_declaration() -> None:
    with pytest.raises(UndeclaredFlag, match="declaration is contract"):
        read_flag(FLAG, AgentSlices.resolve(), plugin="plugin")


def test_read_flag_returns_non_tuning_turn_value(declare) -> None:
    previous = get_field("exec_timeout_seconds")
    try:
        set_field("exec_timeout_seconds", 123.0)
        declare("plugin", ("sandbox.exec_timeout_seconds",))
        assert (
            read_flag("sandbox.exec_timeout_seconds", AgentSlices.resolve(), plugin="plugin")
            == 123.0
        )
    finally:
        set_field("exec_timeout_seconds", previous)


def test_read_flag_resolves_tuning_explicit_value_then_model_default(declare) -> None:
    previous = get_field("prompt_invest_future_enabled")
    try:
        declare("plugin", (FLAG,))
        set_field("prompt_invest_future_enabled", False)
        assert read_flag(FLAG, AgentSlices.resolve(), plugin="plugin") is False
        set_field("prompt_invest_future_enabled", None)
        assert read_flag(FLAG, AgentSlices.resolve(), plugin="plugin") is True
        assert DEFAULT_TUNING.prompt_invest_future_enabled is True
    finally:
        set_field("prompt_invest_future_enabled", previous)


def test_undo_removes_the_plugins_declarations() -> None:
    undo = declare_flags("plugin", (FLAG,))
    assert declared_flags("plugin") == {FLAG}
    undo()

    assert declared_flags("plugin") == frozenset()
    with pytest.raises(UndeclaredFlag):
        read_flag(FLAG, AgentSlices.resolve(), plugin="plugin")


def test_hook_shaped_behavior_can_read_a_declared_flag(declare) -> None:
    previous = get_field("prompt_invest_future_enabled")

    def hook_behavior() -> str:
        if read_flag(FLAG, AgentSlices.resolve(), plugin="plugin"):
            return "include future work"
        return "skip future work"

    try:
        declare("plugin", (FLAG,))
        set_field("prompt_invest_future_enabled", False)
        assert hook_behavior() == "skip future work"
        set_field("prompt_invest_future_enabled", True)
        assert hook_behavior() == "include future work"
    finally:
        set_field("prompt_invest_future_enabled", previous)
