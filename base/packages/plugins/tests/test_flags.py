"""Contract tests for plugin declarations and reads of core configuration flags."""

import pytest

from base.config import get_field, set_field
from base.host.env.agent_slices import AgentSlices
from base.host.env.config_registry import fields
from base.lm.registry import DEFAULT_TUNING
from base.packages.plugins.config_registration import _field_is_sensitive
from base.packages.plugins.context import PluginContext
from base.packages.plugins.flags import (
    NoPluginContext,
    UndeclaredFlag,
    UnknownFlag,
    clear_plugin_flags,
    declare_flags,
    declared_flags,
    read_flag,
)


@pytest.fixture(autouse=True)
def isolated_plugin_flags():
    """Clear declarations around every test because the registry is module state."""
    clear_plugin_flags()
    yield
    clear_plugin_flags()


def test_declare_flags_requires_plugin_context() -> None:
    with pytest.raises(NoPluginContext, match="PluginContext"):
        declare_flags("agent.prompt_invest_future_enabled")


@pytest.mark.parametrize(
    "key",
    ["nodot", "a.b.c", "", "bogus.x", "agent.bogus_field", "data_plane.db_url"],
)
def test_declare_flags_rejects_invalid_or_sensitive_keys(key: str) -> None:
    if key == "data_plane.db_url":
        assert _field_is_sensitive(fields()["db_url"].info.json_schema_extra)
    with PluginContext("plugin"), pytest.raises(UnknownFlag) as exc_info:
        declare_flags(key)
    assert repr(key) in str(exc_info.value)


def test_declare_flags_registers_a_valid_key() -> None:
    with PluginContext("plugin"):
        declare_flags("agent.prompt_invest_future_enabled")

    assert declared_flags("plugin") == {"agent.prompt_invest_future_enabled"}


def test_declarations_are_idempotent_per_plugin_and_shared_across_plugins() -> None:
    key = "agent.prompt_invest_future_enabled"
    with PluginContext("first"):
        declare_flags(key)
        declare_flags(key)
    with PluginContext("second"):
        declare_flags(key)

    assert declared_flags("first") == {key}
    assert declared_flags("second") == {key}
    with PluginContext("first"):
        first_value = read_flag(key, AgentSlices.resolve())
    with PluginContext("second"):
        assert read_flag(key, AgentSlices.resolve()) is first_value


def test_read_flag_requires_plugin_context() -> None:
    with pytest.raises(NoPluginContext, match="pass plugin=<name> explicitly"):
        read_flag("agent.prompt_invest_future_enabled", AgentSlices.resolve())


def test_read_flag_accepts_explicit_plugin_outside_plugin_context() -> None:
    key = "agent.prompt_invest_future_enabled"
    previous = get_field("prompt_invest_future_enabled")
    try:
        set_field("prompt_invest_future_enabled", False)
        with PluginContext("plugin"):
            declare_flags(key)

        assert read_flag(key, AgentSlices.resolve(), plugin="plugin") is False
    finally:
        set_field("prompt_invest_future_enabled", previous)


def test_read_flag_explicit_plugin_wins_over_plugin_context() -> None:
    key = "agent.prompt_invest_future_enabled"
    with PluginContext("declared-plugin"):
        declare_flags(key)

    with PluginContext("other-plugin"):
        assert read_flag(key, AgentSlices.resolve(), plugin="declared-plugin") is True


def test_read_flag_requires_a_declaration() -> None:
    with PluginContext("plugin"), pytest.raises(UndeclaredFlag, match="declaration is contract"):
        read_flag("agent.prompt_invest_future_enabled", AgentSlices.resolve())


def test_read_flag_returns_non_tuning_turn_value() -> None:
    previous = get_field("exec_timeout_seconds")
    try:
        set_field("exec_timeout_seconds", 123.0)
        with PluginContext("plugin"):
            declare_flags("sandbox.exec_timeout_seconds")
            assert read_flag("sandbox.exec_timeout_seconds", AgentSlices.resolve()) == 123.0
    finally:
        set_field("exec_timeout_seconds", previous)


def test_read_flag_resolves_tuning_explicit_value_then_model_default() -> None:
    previous = get_field("prompt_invest_future_enabled")
    try:
        with PluginContext("plugin"):
            declare_flags("agent.prompt_invest_future_enabled")
            set_field("prompt_invest_future_enabled", False)
            assert read_flag("agent.prompt_invest_future_enabled", AgentSlices.resolve()) is False
            set_field("prompt_invest_future_enabled", None)
            assert read_flag("agent.prompt_invest_future_enabled", AgentSlices.resolve()) is True
            assert DEFAULT_TUNING.prompt_invest_future_enabled is True
    finally:
        set_field("prompt_invest_future_enabled", previous)


def test_clear_plugin_flags_removes_declarations() -> None:
    key = "agent.prompt_invest_future_enabled"
    with PluginContext("plugin"):
        declare_flags(key)
    clear_plugin_flags()

    with PluginContext("plugin"), pytest.raises(UndeclaredFlag):
        read_flag(key, AgentSlices.resolve())


def test_hook_shaped_behavior_can_read_declared_flag_outside_plugin_context() -> None:
    previous = get_field("prompt_invest_future_enabled")

    def hook_behavior() -> str:
        if read_flag("agent.prompt_invest_future_enabled", AgentSlices.resolve(), plugin="plugin"):
            return "include future work"
        return "skip future work"

    try:
        with PluginContext("plugin"):
            declare_flags("agent.prompt_invest_future_enabled")
        set_field("prompt_invest_future_enabled", False)
        assert hook_behavior() == "skip future work"
        set_field("prompt_invest_future_enabled", True)
        assert hook_behavior() == "include future work"
    finally:
        set_field("prompt_invest_future_enabled", previous)
