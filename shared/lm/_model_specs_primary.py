"""Model specifications for Claude and GPT."""

from __future__ import annotations

from shared.lm._model_registry_types import (
    _CLAUDE_ADAPTIVE_EFFORT,
    _GPT_EFFORT,
    ModelSpec,
    ModelTuning,
)

PRIMARY_MODELS: dict[str, ModelSpec] = {
    # -- claude --
    "claude-sonnet-5": ModelSpec(
        provider="claude",
        spawnable=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2026-01",
        effort_levels=_CLAUDE_ADAPTIVE_EFFORT,
        tuning=ModelTuning(
            # Pinned 2026-08-01 (user decision, task #568): the picker must
            # show a concrete default, and Anthropic documents `high` as this
            # family's default effort (decisions/2026-07-25-per-model-
            # tuning-values.md Decision 4; "" is exactly equivalent to omitting
            # the parameter, whose default is high). NOT the ladder floor.
            reasoning_effort="high",
            # User decision (2026-09-03): Claude family defaults this section off.
            prompt_user_tone_enabled=False,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    "claude-haiku-4-5-20251001": ModelSpec(
        provider="claude",
        spawnable=True,
        context_window=200_000,
        max_output_tokens=64_000,
        knowledge_cutoff="2025-10",
        # No wire `effort` field (server 400) — the cross-provider knob clamps
        # onto the manual-thinking on/off binary instead.
        effort_levels=("none", "high"),
        extended_thinking_only=True,
        tuning=ModelTuning(
            # Manual extended thinking defaults OFF on this model (budget_tokens
            # default 0) — the honest concrete default is the off rung.
            reasoning_effort="none",
            # User decision (2026-09-03): Claude family defaults this section off.
            prompt_user_tone_enabled=False,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    "claude-opus-5": ModelSpec(
        provider="claude",
        spawnable=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2026-01",
        effort_levels=_CLAUDE_ADAPTIVE_EFFORT,
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): Anthropic documents `high` as the
            # family default (see claude-sonnet-5).
            reasoning_effort="high",
            # User decision (2026-09-03): Claude family defaults this section off.
            prompt_user_tone_enabled=False,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    "claude-fable-5": ModelSpec(
        provider="claude",
        spawnable=True,
        superseded_by="claude-fable-5-1",
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2026-01",
        effort_levels=_CLAUDE_ADAPTIVE_EFFORT,
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): Anthropic documents `high` as the
            # family default (see claude-sonnet-5).
            reasoning_effort="high",
            # User decision (2026-09-03): Claude family defaults this section off.
            prompt_user_tone_enabled=False,
            # Fable 5.x shares one combined rate pool across Fable 5.1 and 5:
            # 25-40% of every other model's ITPM/OTPM at every tier, plus 2.5x
            # fewer RPM above the Start tier, while costing 2x Opus and running
            # the longest turns. More 429s means more provider-SDK internal
            # retries, which are silent on the wire.
            llm_retry_max_attempts=10,
            # That same silence is what TTFT measures: while the SDK retries a
            # 429 internally the socket produces no event at all, so a throttled
            # (but healthy) request looks identical to a hung one at 30s.
            llm_stream_ttft_timeout_seconds=120.0,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    "claude-fable-5-1": ModelSpec(
        provider="claude",
        spawnable=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2026-06",
        effort_levels=_CLAUDE_ADAPTIVE_EFFORT,
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): Anthropic documents `high` as the
            # family default (see claude-sonnet-5).
            reasoning_effort="high",
            # User decision (2026-09-03): Claude family defaults this section off.
            prompt_user_tone_enabled=False,
            # Fable 5.x shares one combined rate pool across Fable 5.1 and 5:
            # 25-40% of every other model's ITPM/OTPM at every tier, plus 2.5x
            # fewer RPM above the Start tier, while costing 2x Opus and running
            # the longest turns. More 429s means more provider-SDK internal
            # retries, which are silent on the wire.
            llm_retry_max_attempts=10,
            # That same silence is what TTFT measures: while the SDK retries a
            # 429 internally the socket produces no event at all, so a throttled
            # (but healthy) request looks identical to a hung one at 30s.
            llm_stream_ttft_timeout_seconds=120.0,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    # -- gpt --
    "gpt-5.6-sol": ModelSpec(
        provider="gpt",
        spawnable=True,
        context_window=1_050_000,
        knowledge_cutoff="2026-02",
        # Flagship tier (explicit id; the bare gpt-5.6 alias also routes here,
        # but the catalog pins the explicit tier id like terra/luna).
        effort_levels=_GPT_EFFORT,
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): OpenAI documents `medium` as the
            # default reasoning effort (decisions/2026-07-25-per-model-
            # tuning-values.md Decision 4).
            reasoning_effort="medium",
        ),
        media_types=frozenset({"image"}),
    ),
    "gpt-5.6-terra": ModelSpec(
        provider="gpt",
        spawnable=True,
        context_window=1_050_000,
        knowledge_cutoff="2026-02",
        effort_levels=_GPT_EFFORT,
        # Same window, same effort ladder across all three tiers — OpenAI
        # documents no per-tier difference in anything Ava tunes.
        tuning=ModelTuning(reasoning_effort="medium"),  # OpenAI default (see gpt-5.6-sol)
        media_types=frozenset({"image"}),
    ),
    "gpt-5.6-luna": ModelSpec(
        provider="gpt",
        spawnable=True,
        context_window=1_050_000,
        knowledge_cutoff="2026-02",
        effort_levels=_GPT_EFFORT,
        tuning=ModelTuning(reasoning_effort="medium"),  # OpenAI default (see gpt-5.6-sol)
        media_types=frozenset({"image"}),
    ),
}
