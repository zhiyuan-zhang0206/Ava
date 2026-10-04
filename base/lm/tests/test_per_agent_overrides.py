"""An agent's explicit tuning values reach the model layering, the context budget and the model build.

The eight per-model-defaultable settings an agent can pin (stream timeouts, reasoning effort,
thinking budget, compaction thresholds) used to resolve against the cluster's value alone, so an
agent's pin was ignored. They now resolve against the agent's `overrides` slice; with no override
the result is what it always was.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_anthropic import ChatAnthropic

from base.config import settings
from base.host.env.agent_slices import AgentSlices, ModelOverrides
from base.lm.context_budget import resolve_context_budget
from base.lm.factory import build_chat_model
from base.lm.plugin_providers import model_catalog
from base.lm.registry import resolve_setting

model_catalog()

_MODEL = "deepseek-flash"

# One pin per overridable setting, each different from the model's own default.
_PINS: dict[str, Any] = {
    "llm_stream_ttft_timeout_seconds": 11.5,
    "llm_stream_total_timeout_seconds": 222.5,
    "llm_stream_inter_chunk_timeout_seconds": 33.5,
    "reasoning_effort": "low",
    "claude_thinking_budget_tokens": 4321,
    "auto_compact_fraction": 0.81,
    "auto_compact_ceiling_tokens": 123_456,
    "compact_reminder_fraction": 0.77,
}


def test_the_pin_table_names_every_overridable_setting() -> None:
    assert set(_PINS) == set(ModelOverrides.__dataclass_fields__)


@pytest.mark.parametrize("setting", sorted(_PINS))
def test_an_agent_pin_is_the_resolved_value(setting: str) -> None:
    overrides = AgentSlices.resolve({setting: _PINS[setting]}).overrides
    assert resolve_setting(setting, model=_MODEL, overrides=overrides) == _PINS[setting]


@pytest.mark.parametrize("setting", sorted(_PINS))
def test_without_a_pin_the_resolution_is_the_cluster_layering(setting: str) -> None:
    unpinned = AgentSlices.resolve().overrides
    expected = resolve_setting(setting, model=_MODEL)
    assert resolve_setting(setting, model=_MODEL, overrides=unpinned) == expected
    assert (
        resolve_setting(setting, model=_MODEL, overrides=ModelOverrides.from_pins({})) == expected
    )


@pytest.mark.parametrize("setting", sorted(_PINS))
def test_a_pin_of_another_setting_leaves_this_one_alone(setting: str) -> None:
    other = next(name for name in sorted(_PINS) if name != setting)
    overrides = AgentSlices.resolve({other: _PINS[other]}).overrides
    expected = resolve_setting(setting, model=_MODEL)
    assert resolve_setting(setting, model=_MODEL, overrides=overrides) == expected


def test_an_agent_pin_wins_over_the_cluster_explicit_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
    overrides = AgentSlices.resolve({"reasoning_effort": "low"}).overrides
    assert resolve_setting("reasoning_effort", model=_MODEL, overrides=overrides) == "low"
    assert resolve_setting("reasoning_effort", model=_MODEL) == "high"


def test_a_setting_outside_the_overrides_ignores_them() -> None:
    overrides = AgentSlices.resolve(_PINS).overrides
    assert resolve_setting(
        "llm_retry_max_attempts", model=_MODEL, overrides=overrides
    ) == resolve_setting("llm_retry_max_attempts", model=_MODEL)


def test_the_context_budget_follows_the_agents_thresholds() -> None:
    plain = resolve_context_budget(_MODEL)
    pinned = resolve_context_budget(
        _MODEL,
        AgentSlices.resolve(
            {"auto_compact_fraction": 0.9, "compact_reminder_fraction": 0.5}
        ).overrides,
    )
    window = plain.max_context_tokens
    assert pinned.hard_compact_tokens == round(0.9 * window)
    assert pinned.soft_compact_tokens == round(0.5 * window)
    assert pinned.hard_compact_tokens != plain.hard_compact_tokens


def test_the_context_budget_follows_the_agents_ceiling() -> None:
    plain = resolve_context_budget(_MODEL)
    ceiling = plain.hard_compact_tokens // 2
    capped = resolve_context_budget(
        _MODEL, AgentSlices.resolve({"auto_compact_ceiling_tokens": ceiling}).overrides
    )
    assert capped.hard_compact_tokens == ceiling
    assert capped.soft_compact_tokens < plain.soft_compact_tokens


def test_without_overrides_the_context_budget_is_unchanged() -> None:
    assert resolve_context_budget(
        _MODEL, AgentSlices.resolve().overrides
    ) == resolve_context_budget(_MODEL)


def test_the_chat_model_is_built_with_the_agents_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
    monkeypatch.setattr(settings.lm, "reasoning_effort", None)
    default = build_chat_model(_MODEL)
    assert isinstance(default, ChatAnthropic)
    assert default.model_kwargs["extra_body"] == {"output_config": {"effort": "max"}}
    overrides = AgentSlices.resolve({"reasoning_effort": "high"}).overrides
    pinned = build_chat_model(_MODEL, overrides=overrides)
    assert isinstance(pinned, ChatAnthropic)
    assert pinned.model_kwargs["extra_body"] == {"output_config": {"effort": "high"}}
    unpinned = build_chat_model(_MODEL, overrides=AgentSlices.resolve().overrides)
    assert isinstance(unpinned, ChatAnthropic)
    assert unpinned.model_kwargs["extra_body"] == default.model_kwargs["extra_body"]


def test_the_chat_model_is_built_with_the_agents_thinking_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(settings.lm, "claude_thinking_budget_tokens", 0)
    monkeypatch.setattr(settings.lm, "reasoning_effort", None)
    haiku = "claude-haiku-4-5-20251001"
    off = build_chat_model(haiku)
    assert isinstance(off, ChatAnthropic)
    assert off.thinking is None
    pinned = build_chat_model(
        haiku, overrides=AgentSlices.resolve({"claude_thinking_budget_tokens": 6000}).overrides
    )
    assert isinstance(pinned, ChatAnthropic)
    assert pinned.thinking == {"type": "enabled", "budget_tokens": 6000}
    unpinned = build_chat_model(haiku, overrides=AgentSlices.resolve().overrides)
    assert isinstance(unpinned, ChatAnthropic)
    assert unpinned.thinking is None
