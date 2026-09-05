"""The per-agent config lifecycle axis (`shared/config`: frozen | live).

The registry's job here is to make an UNDECLARED classification impossible: a new
per-agent field must be ruled on (is this the agent's identity material, or an
operational knob?) before it can load at all. These tests drive `_build_registry`
against synthetic sub-models rather than the real ones, so they assert the rule and
not today's roster.
"""

from __future__ import annotations

import pytest
from pydantic import Field

import shared.config as shared_config
from shared.config._base import EnvSettings


def _registry_with(model: type[EnvSettings]) -> None:
    """Run `_build_registry` over one synthetic domain and nothing else."""
    from shared import config_registry

    domains = (("synthetic", "Synthetic", model, "agent-runner"),)
    original = config_registry._DOMAIN_MODELS
    config_registry._DOMAIN_MODELS = domains
    config_registry._build_registry.cache_clear()
    try:
        config_registry._build_registry()
    finally:
        config_registry._DOMAIN_MODELS = original
        config_registry._build_registry.cache_clear()


class TestRegistryEnforcement:
    def test_per_agent_field_without_lifecycle_is_rejected(self) -> None:
        class Missing(EnvSettings):
            synthetic_knob: int = Field(
                default=1,
                alias="AVA_SYNTHETIC_KNOB",
                json_schema_extra={"per_agent": True, "scope": "cluster-default"},
            )

        with pytest.raises(RuntimeError, match="lifecycle=None"):
            _registry_with(Missing)

    def test_per_agent_field_with_unknown_lifecycle_is_rejected(self) -> None:
        class Bogus(EnvSettings):
            synthetic_knob: int = Field(
                default=1,
                alias="AVA_SYNTHETIC_KNOB",
                json_schema_extra={
                    "per_agent": True,
                    "scope": "cluster-default",
                    "lifecycle": "sticky",
                },
            )

        with pytest.raises(RuntimeError, match="lifecycle='sticky'"):
            _registry_with(Bogus)

    def test_non_per_agent_field_declaring_lifecycle_is_rejected(self) -> None:
        """Cluster-scope config has no per-agent instance to freeze — declaring a
        lifecycle on one is a category error, not a harmless extra key."""

        class Overreach(EnvSettings):
            synthetic_knob: int = Field(
                default=1,
                alias="AVA_SYNTHETIC_KNOB",
                json_schema_extra={"scope": "cluster-pinned", "lifecycle": "frozen"},
            )

        with pytest.raises(RuntimeError, match=r"declares lifecycle='frozen'"):
            _registry_with(Overreach)

    @pytest.mark.parametrize("lifecycle", ["frozen", "live"])
    def test_declared_per_agent_field_loads(self, lifecycle: str) -> None:
        class Fine(EnvSettings):
            synthetic_knob: int = Field(
                default=1,
                alias="AVA_SYNTHETIC_KNOB",
                json_schema_extra={
                    "per_agent": True,
                    "scope": "cluster-default",
                    "lifecycle": lifecycle,
                },
            )

        _registry_with(Fine)


class TestRealRoster:
    def test_every_per_agent_field_is_classified(self) -> None:
        """Belt-and-braces over the enforcement above: the two classes partition the
        real per-agent set exactly, so nothing can be silently in neither."""
        per_agent = shared_config.per_agent_field_names()
        frozen = shared_config.frozen_field_names()
        live = shared_config.live_field_names()
        assert frozen | live == per_agent
        assert not frozen & live

    def test_identity_material_is_frozen(self) -> None:
        """The brain + the system-prompt-shaping set. Compact rebuilds the system
        prompt from current config, so a live default in this set would swap a living
        agent's identity material mid-life."""
        assert shared_config.frozen_field_names() == {
            "llm_model",
            "reasoning_effort",
            "claude_thinking_budget_tokens",
            "skills_to_inject_into_system_prompt",
            "skills_to_expand_at_start",
            "system_prompt_extra",
            "agent_communication_style",
            "sdk_disable",
            "eval_isolation",
            "eval_network_allowlist",
        }

    def test_lifecycle_lookup_rejects_a_non_per_agent_field(self) -> None:
        with pytest.raises(KeyError):
            shared_config.field_lifecycle("labeler_model")
