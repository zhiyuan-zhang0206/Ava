"""An agent's plugin-config view (base/packages/plugins/config_view.py, carried on the agent's
`AgentSlices`) — the plugin-scope half of agent scoping for the hosted runner.

Locks four contracts, one layer over the framework pins:

1. **No pins** — every read is `two_plugins[plugin]` itself, live mutations included.
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

from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.packages.plugins.config_registration import (
    all_plugin_configs,
    bind_plugin_config,
    disk_image_path,
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
def two_plugins(unit_home) -> dict[str, BaseModel]:
    """One root's resolved bindings, with no ambient state."""
    configs: dict[str, BaseModel] = {}
    bind_plugin_config("alpha", _AlphaConfig, configs)
    bind_plugin_config("beta", _BetaConfig, configs)
    return configs


def _slices(
    configs: dict[str, BaseModel], plugin_pins: dict[str, dict[str, object]] | None = None
) -> AgentSlices:
    return AgentSlices.resolve(
        plugin_pins=plugin_pins,
        plugin_configs=configs,
        default_reader=lambda domain, field: getattr(getattr(settings, domain), field),
    )


class TestWithoutPins:
    def test_the_read_is_the_registry_instance(self, two_plugins: dict[str, BaseModel]) -> None:
        assert get_plugin_config("alpha", _slices(two_plugins)) is two_plugins["alpha"]
        assert process_plugin_config("alpha", two_plugins) is two_plugins["alpha"]
        assert all_plugin_configs(_slices(two_plugins)) == two_plugins

    def test_it_sees_a_rebound_instance(self, two_plugins: dict[str, BaseModel]) -> None:
        """Boot rebuilds `two_plugins[plugin]` in place when it applies an
        overlay; an unpinned read must show that, not a snapshot."""
        two_plugins["alpha"] = _AlphaConfig(marker="rebound")
        assert get_plugin_config("alpha", _slices(two_plugins), _AlphaConfig).marker == "rebound"

    def test_unknown_plugin_still_raises_keyerror(self, two_plugins: dict[str, BaseModel]) -> None:
        with pytest.raises(KeyError):
            get_plugin_config("nope", _slices(two_plugins))
        with pytest.raises(KeyError):
            get_plugin_config("nope", _slices(two_plugins, {"alpha": {"marker": "x"}}))


class TestOverrideResolution:
    def test_override_wins_for_its_own_field_only(self, two_plugins: dict[str, BaseModel]) -> None:
        slices = _slices(two_plugins, {"alpha": {"marker": "agent-marker"}})
        alpha = get_plugin_config("alpha", slices, _AlphaConfig)
        assert alpha.marker == "agent-marker"
        assert alpha.threshold == 100
        assert get_plugin_config("beta", slices) is two_plugins["beta"]
        assert get_plugin_config("alpha", _slices(two_plugins)) is two_plugins["alpha"]

    def test_all_plugin_configs_is_agent_scoped(self, two_plugins: dict[str, BaseModel]) -> None:
        configs = all_plugin_configs(_slices(two_plugins, {"alpha": {"marker": "agent-marker"}}))
        assert configs["alpha"].model_dump()["marker"] == "agent-marker"
        assert set(configs) == {"alpha", "beta"}

    def test_built_instance_is_memoized(self, two_plugins: dict[str, BaseModel]) -> None:
        slices = _slices(two_plugins, {"alpha": {"marker": "agent-marker"}})
        assert get_plugin_config("alpha", slices) is get_plugin_config("alpha", slices)

    def test_memoized_instance_follows_a_rebound_class(
        self, two_plugins: dict[str, BaseModel]
    ) -> None:
        """an uninstall + reinstall (the plugin-reload path)
        swaps the class behind a plugin name; a stale cached instance of the
        old class must not survive it."""
        slices = _slices(two_plugins, {"alpha": {"marker": "agent-marker"}})
        assert get_plugin_config("alpha", slices, _AlphaConfig).marker == "agent-marker"
        two_plugins["alpha"] = _BetaConfig()
        assert isinstance(get_plugin_config("alpha", slices), _BetaConfig)

    def test_the_overlay_is_pins_and_plugin_pins_flattened(
        self, two_plugins: dict[str, BaseModel]
    ) -> None:
        slices = AgentSlices.resolve(
            {"llm_model": "m"},
            {"alpha": {"marker": "x"}},
            plugin_configs=two_plugins,
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field),
        )
        assert slices.overlay() == {"llm_model": "m", "marker": "x"}


class TestRouting:
    def test_flat_keys_route_to_their_owner(self, two_plugins: dict[str, BaseModel]) -> None:
        pins = resolve_agent_plugin_pins({"marker": "m", "beta_marker": "b"}, two_plugins)
        assert pins == {"alpha": {"marker": "m"}, "beta": {"beta_marker": "b"}}

    def test_framework_keys_are_not_plugin_pins(self, two_plugins: dict[str, BaseModel]) -> None:
        # llm_model is a framework Settings field — base/config/agent_pins.py
        # owns it; it must not be routed to a plugin.
        assert resolve_agent_plugin_pins({"llm_model": "x"}, two_plugins) == {}

    def test_unknown_key_is_dropped_not_raised(self, two_plugins: dict[str, BaseModel]) -> None:
        assert resolve_agent_plugin_pins({"gone_in_a_later_release": 1}, two_plugins) == {}

    def test_ambiguous_key_is_dropped(self, two_plugins: dict[str, BaseModel]) -> None:
        """A key two plugins both declare could never have been stored —
        `resolve_overlay_targets` rejects it at write time — so the read side
        drops it instead of guessing an owner."""
        two_plugins["beta"] = _AlphaConfig()
        assert resolve_agent_plugin_pins({"marker": "m"}, two_plugins) == {}

    def test_empty_overlay_resolves_empty(self, two_plugins: dict[str, BaseModel]) -> None:
        assert resolve_agent_plugin_pins(None, two_plugins) == {}
        assert resolve_agent_plugin_pins({}, two_plugins) == {}


class TestIsolation:
    def test_two_agents_read_their_own_config(self, two_plugins: dict[str, BaseModel]) -> None:
        """The hosted invariant: two agents, one process, one `two_plugins`; each agent's
        slices read its own override."""
        a = _slices(two_plugins, {"alpha": {"marker": "agent-a"}})
        b = _slices(two_plugins, {"alpha": {"marker": "agent-b"}})
        assert get_plugin_config("alpha", a, _AlphaConfig).marker == "agent-a"
        assert get_plugin_config("alpha", b, _AlphaConfig).marker == "agent-b"
        assert get_plugin_config("alpha", a, _AlphaConfig).marker == "agent-a"


def test_independent_roots_bind_the_same_plugin_without_sharing_values(
    two_plugins: dict[str, BaseModel],
) -> None:
    second: dict[str, BaseModel] = {}
    disk_image_path("alpha").write_text(_AlphaConfig(marker="second-root").model_dump_json())
    bind_plugin_config("alpha", _AlphaConfig, second)

    first_agent = _slices(two_plugins)
    second_agent = _slices(second)
    assert get_plugin_config("alpha", first_agent, _AlphaConfig).marker == ".git"
    assert get_plugin_config("alpha", second_agent, _AlphaConfig).marker == "second-root"
    assert get_plugin_config("alpha", first_agent) is two_plugins["alpha"]
    assert get_plugin_config("alpha", second_agent) is second["alpha"]
