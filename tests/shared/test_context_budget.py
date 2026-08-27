"""Tests for `shared/lm/context_budget.py` — per-model compaction thresholds
derived from each model's context window, and the provider-truth occupancy read.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from shared.config import settings
from shared.lm.context_budget import (
    UnknownModelWindowError,
    latest_input_tokens,
    resolve_context_budget,
)
from shared.lm.factory import MODEL_CONTEXT_WINDOW, SUPPORTED_MODELS


def test_budget_is_thirty_forty_percent_of_a_1m_window() -> None:
    """The roster-wide rule on a 1M-window model: remind at 30%, force-compact
    at 40% of the window."""
    budget = resolve_context_budget("claude-sonnet-5")
    assert budget.max_context_tokens == 1_000_000
    assert budget.hard_compact_tokens == 400_000
    assert budget.soft_compact_tokens == 300_000


def test_deepseek_budget_is_600k_soft_700k_hard() -> None:
    """User decision (task #581): the deepseek entries opt out of the flat rule
    with per-model fractions — soft 600k / hard 700k on their 1M window."""
    for model in ("deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"):
        budget = resolve_context_budget(model)
        assert budget.max_context_tokens == 1_000_000
        assert budget.soft_compact_tokens == 600_000, model
        assert budget.hard_compact_tokens == 700_000, model


def test_budget_scales_to_a_smaller_window() -> None:
    """Same 30/40 rule on a 200K-window model — the whole point of expressing it
    as a fraction: one absolute token count was unreachable here (never
    compacted) while being far too loose on a 1M model."""
    budget = resolve_context_budget("claude-haiku-4-5-20251001")
    assert budget.max_context_tokens == 200_000
    assert budget.hard_compact_tokens == 80_000
    assert budget.soft_compact_tokens == 60_000


def test_every_non_deepseek_spawnable_model_runs_the_flat_thirty_forty_rule() -> None:
    """The roster carries no per-model compact fraction or ceiling, so EVERY
    model's thresholds are exactly 30% / 40% of its own context window — except
    the deepseek entries, which carry the user-pinned 0.6 / 0.7 (see
    test_deepseek_budget_is_600k_soft_700k_hard)."""
    for models in SUPPORTED_MODELS.values():
        for model in models:
            if model in ("deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"):
                continue
            budget = resolve_context_budget(model)
            window = budget.max_context_tokens
            assert budget.soft_compact_tokens == round(0.3 * window), model
            assert budget.hard_compact_tokens == round(0.4 * window), model


def test_fractions_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The thresholds track the configured fractions (per-agent overridable)."""
    monkeypatch.setattr(settings.agent, "auto_compact_fraction", 0.5)
    monkeypatch.setattr(settings.agent, "compact_reminder_fraction", 0.25)
    budget = resolve_context_budget("deepseek-v4-pro")
    assert budget.hard_compact_tokens == 500_000
    assert budget.soft_compact_tokens == 250_000


def test_ceiling_caps_the_hard_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the absolute cap: on a 1M-window model the fraction
    alone lands at 800K, far past where any lab triggers its own agent's
    compaction. The ceiling wins whenever it is the smaller of the two."""
    monkeypatch.setattr(settings.agent, "auto_compact_fraction", 0.8)
    monkeypatch.setattr(settings.agent, "compact_reminder_fraction", 0.6)
    monkeypatch.setattr(settings.agent, "auto_compact_ceiling_tokens", 150_000)
    budget = resolve_context_budget("deepseek-v4-pro")
    assert budget.hard_compact_tokens == 150_000
    # The reminder is compressed by the same 150K/800K factor, so it keeps its
    # 0.75 lead instead of sitting above the forced ceiling at 600K.
    assert budget.soft_compact_tokens == 112_500


def test_ceiling_above_the_fraction_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ceiling that never binds must leave both thresholds byte-identical to
    the pure-fraction result — a model whose evidence says "no cap needed"
    carries 0 and behaves exactly as before."""
    monkeypatch.setattr(settings.agent, "auto_compact_fraction", 0.8)
    monkeypatch.setattr(settings.agent, "compact_reminder_fraction", 0.6)
    monkeypatch.setattr(settings.agent, "auto_compact_ceiling_tokens", 900_000)
    capped = resolve_context_budget("deepseek-v4-pro")
    monkeypatch.setattr(settings.agent, "auto_compact_ceiling_tokens", 0)
    uncapped = resolve_context_budget("deepseek-v4-pro")
    assert capped == uncapped
    assert uncapped.hard_compact_tokens == 800_000
    assert uncapped.soft_compact_tokens == 600_000


def test_soft_stays_below_hard_for_every_spawnable_model() -> None:
    """Registry invariant across the whole roster: the reminder must fire
    strictly before the forced compaction, whatever combination of per-model
    fraction and ceiling the entry carries."""
    for models in SUPPORTED_MODELS.values():
        for model in models:
            budget = resolve_context_budget(model)
            assert 0 < budget.soft_compact_tokens < budget.hard_compact_tokens, model
            assert budget.hard_compact_tokens <= budget.max_context_tokens, model


def test_unknown_model_raises() -> None:
    """A model with no MODEL_CONTEXT_WINDOW entry cannot have thresholds derived —
    fail-fast rather than borrow a wrong window."""
    with pytest.raises(UnknownModelWindowError, match="no-such-model"):
        resolve_context_budget("no-such-model")


def test_every_supported_model_resolves() -> None:
    """Registry invariant: every spawnable model has a context window, so the
    compact hook's resolve never raises for a legitimately-spawned agent. This
    is what lets the hook let UnknownModelWindowError surface (it only fires on a
    developer registry gap, which this test catches in CI, not prod)."""
    for models in SUPPORTED_MODELS.values():
        for model in models:
            assert model in MODEL_CONTEXT_WINDOW, (
                f"{model} is spawnable but missing from MODEL_CONTEXT_WINDOW — "
                f"add it so its compaction thresholds can be derived"
            )
            # And it actually resolves without raising.
            resolve_context_budget(model)


def test_latest_input_tokens_reads_most_recent_usage() -> None:
    """Newest-first scan: the last AIMessage carrying usage_metadata wins."""
    messages = [
        SystemMessage(content="sys"),
        AIMessage(
            content="a",
            usage_metadata={"input_tokens": 100, "output_tokens": 5, "total_tokens": 105},
        ),
        HumanMessage(content="hi"),
        AIMessage(
            content="b",
            usage_metadata={"input_tokens": 250, "output_tokens": 9, "total_tokens": 259},
        ),
    ]
    assert latest_input_tokens(messages) == 250


def test_latest_input_tokens_none_before_first_call() -> None:
    """No AIMessage with usage yet (just-spawned agent / post-compaction) -> None
    so the caller falls back to a chars/4 estimate."""
    messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]
    assert latest_input_tokens(messages) is None
    # An AIMessage without usage_metadata is skipped, not counted as 0.
    assert latest_input_tokens([AIMessage(content="no usage")]) is None
