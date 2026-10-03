"""An agent's plugin-config view (base/packages/plugins/config_view.py, carried on the agent's
`AgentSlices`) — the plugin-scope half of agent scoping for the hosted runner.

Locks four contracts, one layer over the framework pins:

1. **No pins** — every read is `_PLUGIN_CONFIGS[plugin]` itself, live mutations included.
2. **Override resolution** — an agent's override wins for its own plugin+field only; other
   fields keep the disk image's values, other plugins are untouched; the built instance is
   memoized per agent.
3. **Routing** — flat overlay keys land on their owning plugin; framework keys, unknown keys and
   ambiguous keys are dropped rather than raised.
4. **Isolation** — two agents' slices read their own config from the one process-global map.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field

from base.host.env.agent_slices import AgentSlices
from base.packages.plugins.config_registration import (
    _PLUGIN_CONFIG_CLASSES,
    _PLUGIN_CONFIGS,
    all_plugin_configs,
    bind_plugin_config,
    get_plugin_config,
    process_plugin_config,
)
from base.packages.plugins.config_view import resolve_agent_plugin_pins


class _AlphaConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    marker: str = Field(default=".git", json_schema_extra={"per_agent": True})
    threshold: int = Field(default=100)


class _BetaConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    beta_marker: str = Field(default="beta-default", json_schema_extra={"per_agent": True})


@pytest.fixture
def two_plugins(unit_home):
    """A registry holding exactly two plugins, restored afterwards."""
    snap_classes = dict(_PLUGIN_CONFIG_CLASSES)
    snap_configs = dict(_PLUGIN_CONFIGS)
    _PLUGIN_CONFIG_CLASSES.clear()
    _PLUGIN_CONFIGS.clear()
    bind_plugin_config("alpha", _AlphaConfig)
    bind_plugin_config("beta", _BetaConfig)
    yield
    _PLUGIN_CONFIG_CLASSES.clear()
    _PLUGIN_CONFIGS.clear()
    _PLUGIN_CONFIG_CLASSES.update(snap_classes)
    _PLUGIN_CONFIGS.update(snap_configs)


def _slices(plugin_pins: dict[str, dict[str, object]] | None = None) -> AgentSlices:
    return AgentSlices.resolve(plugin_pins=plugin_pins)


class TestWithoutPins:
    def test_the_read_is_the_registry_instance(self, two_plugins) -> None:
        assert get_plugin_config("alpha", _slices()) is _PLUGIN_CONFIGS["alpha"]
        assert process_plugin_config("alpha") is _PLUGIN_CONFIGS["alpha"]
        assert all_plugin_configs(_slices()) == _PLUGIN_CONFIGS

    def test_it_sees_a_rebound_instance(self, two_plugins) -> None:
        """Boot rebuilds `_PLUGIN_CONFIGS[plugin]` in place when it applies an
        overlay; an unpinned read must show that, not a snapshot."""
        _PLUGIN_CONFIGS["alpha"] = _AlphaConfig(marker="rebound")
        assert get_plugin_config("alpha", _slices(), _AlphaConfig).marker == "rebound"

    def test_unknown_plugin_still_raises_keyerror(self, two_plugins) -> None:
        with pytest.raises(KeyError):
            get_plugin_config("nope", _slices())
        with pytest.raises(KeyError):
            get_plugin_config("nope", _slices({"alpha": {"marker": "x"}}))


class TestOverrideResolution:
    def test_override_wins_for_its_own_field_only(self, two_plugins) -> None:
        slices = _slices({"alpha": {"marker": "agent-marker"}})
        alpha = get_plugin_config("alpha", slices, _AlphaConfig)
        assert alpha.marker == "agent-marker"
        assert alpha.threshold == 100
        assert get_plugin_config("beta", slices) is _PLUGIN_CONFIGS["beta"]
        assert get_plugin_config("alpha", _slices()) is _PLUGIN_CONFIGS["alpha"]

    def test_all_plugin_configs_is_agent_scoped(self, two_plugins) -> None:
        configs = all_plugin_configs(_slices({"alpha": {"marker": "agent-marker"}}))
        assert configs["alpha"].model_dump()["marker"] == "agent-marker"
        assert set(configs) == {"alpha", "beta"}

    def test_built_instance_is_memoized(self, two_plugins) -> None:
        slices = _slices({"alpha": {"marker": "agent-marker"}})
        assert get_plugin_config("alpha", slices) is get_plugin_config("alpha", slices)

    def test_memoized_instance_follows_a_rebound_class(self, two_plugins) -> None:
        """an uninstall + reinstall (the plugin-reload path)
        swaps the class behind a plugin name; a stale cached instance of the
        old class must not survive it."""
        slices = _slices({"alpha": {"marker": "agent-marker"}})
        assert get_plugin_config("alpha", slices, _AlphaConfig).marker == "agent-marker"
        _PLUGIN_CONFIGS["alpha"] = _BetaConfig()
        assert isinstance(get_plugin_config("alpha", slices), _BetaConfig)

    def test_the_overlay_is_pins_and_plugin_pins_flattened(self, two_plugins) -> None:
        slices = AgentSlices.resolve({"llm_model": "m"}, {"alpha": {"marker": "x"}})
        assert slices.overlay() == {"llm_model": "m", "marker": "x"}


class TestRouting:
    def test_flat_keys_route_to_their_owner(self, two_plugins) -> None:
        pins = resolve_agent_plugin_pins({"marker": "m", "beta_marker": "b"})
        assert pins == {"alpha": {"marker": "m"}, "beta": {"beta_marker": "b"}}

    def test_framework_keys_are_not_plugin_pins(self, two_plugins) -> None:
        # llm_model is a framework Settings field — base/config/agent_pins.py
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


class TestIsolation:
    def test_two_agents_read_their_own_config(self, two_plugins) -> None:
        """The hosted invariant: two agents, one process, one `_PLUGIN_CONFIGS`; each agent's
        slices read its own override."""
        a = _slices({"alpha": {"marker": "agent-a"}})
        b = _slices({"alpha": {"marker": "agent-b"}})
        assert get_plugin_config("alpha", a, _AlphaConfig).marker == "agent-a"
        assert get_plugin_config("alpha", b, _AlphaConfig).marker == "agent-b"
        assert get_plugin_config("alpha", a, _AlphaConfig).marker == "agent-a"
