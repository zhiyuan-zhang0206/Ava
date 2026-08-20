"""Per-turn plugin-config view (shared/plugin_config_view.py) — the plugin-scope
half of agent scoping for the hosted runner
(future/infra/agent-runner-as-server.md, work item b).

Locks the same four contracts `tests/shared/test_turn_config_view.py` locks for
framework Settings, one layer over:

1. **Process-mode equivalence** — with nothing bound, every read is
   `_PLUGIN_CONFIGS[plugin]` itself, live mutations included.
2. **Override resolution** — a bound override wins for its own plugin+field
   only; other fields keep the disk image's values, other plugins are untouched.
3. **Routing** — flat overlay keys land on their owning plugin; framework keys,
   unknown keys and ambiguous keys are dropped rather than raised.
4. **Isolation + propagation** — two concurrently bound agents read their own
   config, and a bind before `asyncio.create_task` reaches the task and the
   tasks it spawns (the LangGraph node-task shape).
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, ConfigDict, Field

from shared.plugin_config_registry import (
    _PLUGIN_CONFIG_CLASSES,
    _PLUGIN_CONFIGS,
    all_plugin_configs,
    clear_plugin_configs,
    get_plugin_config,
)
from shared.plugin_config_view import (
    bind_agent_plugin_config,
    current_plugin_config_view,
    resolve_agent_plugin_pins,
)


class _AlphaConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    marker: str = Field(default=".git", json_schema_extra={"per_agent": True})
    threshold: int = Field(default=100)


class _BetaConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    beta_marker: str = Field(default="beta-default", json_schema_extra={"per_agent": True})


@pytest.fixture
def two_plugins():
    """A registry holding exactly two plugins, restored afterwards."""
    snap_classes = dict(_PLUGIN_CONFIG_CLASSES)
    snap_configs = dict(_PLUGIN_CONFIGS)
    clear_plugin_configs()
    _PLUGIN_CONFIG_CLASSES.update({"alpha": _AlphaConfig, "beta": _BetaConfig})
    _PLUGIN_CONFIGS.update({"alpha": _AlphaConfig(), "beta": _BetaConfig()})
    yield
    clear_plugin_configs()
    _PLUGIN_CONFIG_CLASSES.update(snap_classes)
    _PLUGIN_CONFIGS.update(snap_configs)


class TestProcessModeEquivalence:
    def test_unbound_read_is_the_registry_instance(self, two_plugins) -> None:
        assert current_plugin_config_view() is None
        assert get_plugin_config("alpha") is _PLUGIN_CONFIGS["alpha"]
        assert all_plugin_configs() == _PLUGIN_CONFIGS

    def test_unbound_sees_a_rebound_instance(self, two_plugins) -> None:
        """Boot rebuilds `_PLUGIN_CONFIGS[plugin]` in place when it applies an
        overlay; an unbound read must show that, not a snapshot."""
        _PLUGIN_CONFIGS["alpha"] = _AlphaConfig(marker="rebound")
        assert get_plugin_config("alpha", _AlphaConfig).marker == "rebound"

    def test_unknown_plugin_still_raises_keyerror(self, two_plugins) -> None:
        with pytest.raises(KeyError):
            get_plugin_config("nope")
        with bind_agent_plugin_config({"alpha": {"marker": "x"}}), pytest.raises(KeyError):
            get_plugin_config("nope")


class TestOverrideResolution:
    def test_override_wins_for_its_own_field_only(self, two_plugins) -> None:
        with bind_agent_plugin_config({"alpha": {"marker": "agent-marker"}}):
            alpha = get_plugin_config("alpha", _AlphaConfig)
            assert alpha.marker == "agent-marker"
            assert alpha.threshold == 100
            assert get_plugin_config("beta") is _PLUGIN_CONFIGS["beta"]
        assert get_plugin_config("alpha") is _PLUGIN_CONFIGS["alpha"]

    def test_all_plugin_configs_is_agent_scoped(self, two_plugins) -> None:
        with bind_agent_plugin_config({"alpha": {"marker": "agent-marker"}}):
            configs = all_plugin_configs()
            assert configs["alpha"].model_dump()["marker"] == "agent-marker"
            assert set(configs) == {"alpha", "beta"}

    def test_built_instance_is_memoized(self, two_plugins) -> None:
        with bind_agent_plugin_config({"alpha": {"marker": "agent-marker"}}):
            assert get_plugin_config("alpha") is get_plugin_config("alpha")

    def test_memoized_instance_follows_a_rebound_class(self, two_plugins) -> None:
        """`clear_plugin_configs` + re-register (the test/plugin-reload path)
        swaps the class behind a plugin name; a stale cached instance of the
        old class must not survive it."""
        with bind_agent_plugin_config({"alpha": {"marker": "agent-marker"}}):
            assert get_plugin_config("alpha", _AlphaConfig).marker == "agent-marker"
            _PLUGIN_CONFIGS["alpha"] = _BetaConfig()
            assert isinstance(get_plugin_config("alpha"), _BetaConfig)


class TestRouting:
    def test_flat_keys_route_to_their_owner(self, two_plugins) -> None:
        pins = resolve_agent_plugin_pins({"marker": "m", "beta_marker": "b"})
        assert pins == {"alpha": {"marker": "m"}, "beta": {"beta_marker": "b"}}

    def test_framework_keys_are_not_plugin_pins(self, two_plugins) -> None:
        # llm_model is a framework Settings field — shared/config/turn_view.py
        # owns it; it must not be routed to a plugin.
        assert resolve_agent_plugin_pins({"llm_model": "x"}) == {}

    def test_unknown_key_is_dropped_not_raised(self, two_plugins) -> None:
        assert resolve_agent_plugin_pins({"gone_in_a_later_release": 1}) == {}

    def test_ambiguous_key_is_dropped(self, two_plugins) -> None:
        """A key two plugins both declare could never have been stored —
        `resolve_overlay_targets` rejects it at write time — so the read side
        drops it instead of guessing an owner."""
        _PLUGIN_CONFIG_CLASSES["beta"] = _AlphaConfig
        assert resolve_agent_plugin_pins({"marker": "m"}) == {}

    def test_empty_overlay_resolves_empty(self, two_plugins) -> None:
        assert resolve_agent_plugin_pins(None) == {}
        assert resolve_agent_plugin_pins({}) == {}


class TestIsolationAndPropagation:
    async def test_two_agents_read_their_own_config(self, two_plugins) -> None:
        """The hosted invariant: concurrent turns of two agents, one process,
        one `_PLUGIN_CONFIGS`. Each turn's task must read its own override."""
        seen: dict[str, str] = {}

        async def turn(name: str) -> None:
            seen[name] = get_plugin_config("alpha", _AlphaConfig).marker
            await asyncio.sleep(0)  # hand the loop to the other agent's turn
            seen[name + "-after"] = get_plugin_config("alpha", _AlphaConfig).marker

        with bind_agent_plugin_config({"alpha": {"marker": "agent-a"}}):
            task_a = asyncio.create_task(turn("a"))
        with bind_agent_plugin_config({"alpha": {"marker": "agent-b"}}):
            task_b = asyncio.create_task(turn("b"))
        await asyncio.gather(task_a, task_b)

        assert seen == {
            "a": "agent-a",
            "a-after": "agent-a",
            "b": "agent-b",
            "b-after": "agent-b",
        }

    async def test_bind_reaches_nested_tasks(self, two_plugins) -> None:
        """LangGraph runs each node in its own task copying the loop-level
        context — a bind before the turn task therefore covers every node."""
        result: dict[str, str] = {}

        async def node() -> None:
            result["node"] = get_plugin_config("alpha", _AlphaConfig).marker

        async def turn() -> None:
            await asyncio.create_task(node())

        with bind_agent_plugin_config({"alpha": {"marker": "agent-a"}}):
            task = asyncio.create_task(turn())
        await task
        assert result["node"] == "agent-a"

    async def test_sibling_task_outside_the_bind_is_unaffected(self, two_plugins) -> None:
        outside: dict[str, object] = {}

        async def sibling() -> None:
            outside["view"] = current_plugin_config_view()
            outside["marker"] = get_plugin_config("alpha", _AlphaConfig).marker

        with bind_agent_plugin_config({"alpha": {"marker": "agent-a"}}):
            pass
        await asyncio.create_task(sibling())
        assert outside == {"view": None, "marker": ".git"}
