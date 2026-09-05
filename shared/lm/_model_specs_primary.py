"""Model specifications for DeepSeek, Claude, Gemini, and GPT."""

from __future__ import annotations

from shared.lm._model_registry_types import (
    _CLAUDE_ADAPTIVE_EFFORT,
    _GPT_EFFORT,
    ModelSpec,
    ModelTuning,
)

PRIMARY_MODELS: dict[str, ModelSpec] = {
    # -- deepseek --
    "deepseek-v4-pro": ModelSpec(
        provider="deepseek",
        spawnable=True,
        context_window=1_000_000,
        max_output_tokens=384_000,
        knowledge_cutoff="2026-04",
        model_identity="You are running on DeepSeek V4 Pro.",
        effort_levels=("high", "max"),
        tuning=ModelTuning(
            # DeepSeek defaults to `high` and only auto-promotes to `max` for
            # harnesses it recognizes (it names Claude Code / OpenCode); Ava is
            # not on that list, so the promotion has to be explicit. Their own
            # V4 report has max beating high on EVERY agentic metric.
            reasoning_effort="max",
            # DeepSeek documents queueing a request for up to 10 minutes while
            # emitting only SSE COMMENT frames (`: keep-alive`), which no SDK
            # surfaces as a chunk — so a healthy queued request is indistinguish-
            # able from a dead one until data starts. 600 matches the server's
            # own connection-close cutoff.
            llm_stream_ttft_timeout_seconds=600.0,
            # Compact thresholds pinned 2026-08-29 (user decision, reverting
            # the 2026-08-27 600k/700k pin): soft 374k / hard 512k — the task
            # #581 values. 0.512 / 0.374 of the 1M window is exactly
            # 512_000 / 374_000
            # (decisions/2026-08-29-deepseek-compact-thresholds-374k-512k.md).
            auto_compact_fraction=0.512,
            compact_reminder_fraction=0.374,
        ),
    ),
    "deepseek-v4-flash": ModelSpec(
        provider="deepseek",
        spawnable=True,
        context_window=1_000_000,
        max_output_tokens=384_000,
        knowledge_cutoff="2026-04",
        model_identity="You are running on DeepSeek V4 Flash.",
        effort_levels=("high", "max"),
        tuning=ModelTuning(
            reasoning_effort="max",  # same as pro: Ava is not an auto-promoted harness
            llm_stream_ttft_timeout_seconds=600.0,  # same documented 10-minute queue
            # Same decision as deepseek-v4-pro (2026-08-29): soft 374k /
            # hard 512k on the 1M window — 0.512 / 0.374 exactly.
            auto_compact_fraction=0.512,
            compact_reminder_fraction=0.374,
        ),
    ),
    # DeepSeek's experimental multimodal variant of v4-flash (api-docs.deepseek.com/
    # guides/vision, 2026-08-21): same text capabilities, window, output cap and
    # rates as v4-flash; adds still-image input (JPEG/PNG/GIF/WebP, no video/
    # audio) on every API surface, including the anthropic-compatible endpoint
    # Ava binds. Images bill as input tokens (<=384 per image) at v4-flash rates.
    "deepseek-v4-flash-vision-exp": ModelSpec(
        provider="deepseek",
        spawnable=True,
        context_window=1_000_000,
        max_output_tokens=384_000,
        # No vision-specific cutoff published; carries the v4 family's value.
        knowledge_cutoff="2026-04",
        model_identity="You are running on DeepSeek V4 Flash Vision (experimental).",
        effort_levels=("high", "max"),
        media_types=frozenset({"image"}),
        tuning=ModelTuning(
            reasoning_effort="max",  # same as pro/flash: Ava is not an auto-promoted harness
            llm_stream_ttft_timeout_seconds=600.0,  # same documented 10-minute queue
            # Same decision as deepseek-v4-pro (2026-08-29): soft 374k /
            # hard 512k on the 1M window — 0.512 / 0.374 exactly.
            auto_compact_fraction=0.512,
            compact_reminder_fraction=0.374,
        ),
    ),
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
    # -- gemini --
    "gemini-3.8-flash": ModelSpec(
        provider="gemini",
        unavailable_fallback="gemini-3.7-flash",
        context_window=1_048_576,
        # Google does not publish a cutoff for 3.8 Flash; carries the 3.7
        # estimate forward (3.7 GA'd 2026-08-13, 3.8 GA'd 2026-09-02).
        knowledge_cutoff="2026-03",
        # thinking_level vocabulary; `minimal` 400s (verified live 2026-09-03).
        effort_levels=("low", "medium", "high"),
        tuning=ModelTuning(
            # The model page says its default thinking_level is `medium`
            # (decisions/2026-07-25-per-model-tuning-values.md).
            reasoning_effort="medium",
        ),
        media_types=frozenset({"image", "pdf", "audio", "video"}),
    ),
    "gemini-3.7-flash": ModelSpec(
        provider="gemini",
        spawnable=True,
        context_window=1_048_576,
        knowledge_cutoff="2026-03",
        effort_levels=("minimal", "low", "medium", "high"),
        tuning=ModelTuning(reasoning_effort="medium"),
        media_types=frozenset({"image", "pdf", "audio", "video"}),
    ),
    "gemini-3.5-flash": ModelSpec(
        provider="gemini",
        spawnable=True,
        context_window=1_048_576,
        knowledge_cutoff="2025-01",
        effort_levels=("minimal", "low", "medium", "high"),
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): flash default thinking_level is
            # `medium` (see gemini-3.8-flash).
            reasoning_effort="medium",
        ),
        media_types=frozenset({"image", "pdf", "audio", "video"}),
    ),
    "gemini-3.1-pro-preview": ModelSpec(
        provider="gemini",
        spawnable=True,
        # 1,048,576 per three independent first-party Google pages (the model
        # page, the thinking guide, and the DeepMind model card). Every "2M"
        # claim traces back to speculative blogs about a rumored Ultra tier.
        context_window=1_048_576,
        knowledge_cutoff="2025-01",
        # `minimal` returns 400 (verified live 2026-09-03); matches the
        # recorded decision that this model cannot drop to minimal.
        effort_levels=("low", "medium", "high"),
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): this model defaults to
            # thinking_level=high and cannot drop to minimal (Google docs;
            # also recorded in decisions/2026-07-25-per-model-tuning-
            # values.md Decision 3).
            reasoning_effort="high",
            # The only asymmetry Google's own docs admit inside this family:
            # preview models "might come with more restrictive rate limits" and
            # "rate limits are more restricted for experimental and preview
            # models"; the two flashes are GA. Matching first-party 503 reports
            # against paid accounts since launch.
            llm_retry_max_attempts=10,
            # Gemini's wire has no protocol preamble (no message_start /
            # response.created), so the first chunk IS the first content or
            # thought — unlike Claude and GPT, thinking time lands inside TTFT
            # here. This model defaults to thinking_level=high and cannot go to
            # `minimal`, and first-output latency of 17s+ was observed during a
            # Vertex degradation.
            llm_stream_ttft_timeout_seconds=90.0,
        ),
        media_types=frozenset({"image", "pdf", "audio", "video"}),
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
