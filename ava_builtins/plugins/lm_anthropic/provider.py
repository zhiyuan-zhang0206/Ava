"""Anthropic Claude provider plugin for Ava."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from loguru import logger

from shared.lm._effort import _clamp_effort
from shared.lm.provider_api import (
    AttachPolicy,
    BuildContext,
    PricePeriod,
    PriceRates,
    PriceTier,
    ProviderBinding,
    register,
    require_key,
)
from shared.lm.registry import MODELS, ModelSpec, ModelTuning, resolve_setting
from shared.lm.stop import StopSpec

# Budget used when AVA_REASONING_EFFORT clamps an extended-thinking-only claude
# model to its "on" tier and no explicit AVA_CLAUDE_THINKING_BUDGET_TOKENS is
# configured (0 = unset). Anthropic's minimum is 1024; this sits well under
# haiku-4-5's 64K max_tokens cap with headroom left for the actual response.
_CLAUDE_EXTENDED_THINKING_DEFAULT_BUDGET = 8192

# Effort vocabulary shared by adaptive-thinking Claude models. The per-model
# declarations remain authoritative because sonnet-4-6 and haiku-4-5 diverge.
_CLAUDE_ADAPTIVE_EFFORT = ("low", "medium", "high", "xhigh", "max")


def claude_extended_thinking_kwarg(
    model: str,
    *,
    thinking: Mapping[str, Any] | None,
    budget_tokens: int,
    reasoning_effort: str,
) -> dict[str, Any] | None:
    """Resolve the `thinking` kwarg for extended-thinking-only claude models
    (`ModelSpec.extended_thinking_only`, currently haiku-4-5) when the caller
    passed none explicitly.

    `budget_tokens` (the resolved claude_thinking_budget_tokens, an explicit
    numeric budget) wins when set; otherwise `reasoning_effort` clamped onto
    the model's binary `effort_levels` ("none"/"high") opts in at
    `_CLAUDE_EXTENDED_THINKING_DEFAULT_BUDGET` — these models have no `effort`
    wire field, so this is the knob's only effect on them. None = leave
    thinking unset (provider default OFF); also None for any model that is not
    extended-thinking-only or when the caller already set `thinking`.
    """
    spec = MODELS.get(model)
    if thinking is not None or spec is None or not spec.extended_thinking_only:
        return None
    if budget_tokens > 0:
        return {"type": "enabled", "budget_tokens": budget_tokens}
    if reasoning_effort:
        levels = spec.effort_levels
        if levels is not None and _clamp_effort(reasoning_effort, levels, target=model) != "none":
            return {"type": "enabled", "budget_tokens": _CLAUDE_EXTENDED_THINKING_DEFAULT_BUDGET}
    return None


def build(ctx: BuildContext) -> BaseChatModel:
    """claude-* branch: ThinkingTokensChatAnthropic, pinned max_tokens.

    Extended-thinking-only models (haiku-4-5) map budget_tokens / effort
    onto their thinking on/off binary (`claude_extended_thinking_kwarg`);
    adaptive-thinking models get `display: "summarized"` forced on so the
    wire returns thinking text the timeline can render. Reasoning effort
    rides the `effort` field, gated per model.
    """
    from shared.lm._anthropic_compat import ThinkingTokensChatAnthropic

    # Fail fast on missing key — the same posture as every other provider
    # branch. Without it, ChatAnthropic reads ANTHROPIC_API_KEY from env
    # (or not) and hangs rather than surfacing a clear error.
    api_key = require_key("ANTHROPIC_API_KEY")

    extra_kwargs: dict[str, Any] = {}
    if ctx.thinking is not None:
        extra_kwargs["thinking"] = ctx.thinking
    if ctx.disable_streaming:
        extra_kwargs["disable_streaming"] = True

    # Per-model output cap (ModelSpec.max_output_tokens). Same fail-fast
    # posture as the deepseek branch — an unregistered claude model raises
    # rather than falling back to langchain's stale profile table (4096
    # for unknown ids).
    spec = ctx.spec
    if spec is None or spec.max_output_tokens is None:
        raise ValueError(
            f"Unknown claude model {ctx.model!r} — register it (with "
            f"max_output_tokens) in `shared/lm/registry.py:MODELS`"
        )

    thinking_disabled = ctx.thinking is not None and ctx.thinking.get("type") == "disabled"

    # Reasoning effort rides ChatAnthropic's `effort` field
    # (output_config.effort on the wire), gated per model — extended-
    # thinking-only models (haiku-4-5) have no effort support (server 400)
    # and map the knob onto their thinking on/off binary below instead.
    # Caller-disabled thinking skips effort injection, mirroring deepseek:
    # an explicit cheap/fast path shouldn't have the global env push
    # reasoning back in.
    claude_kwargs: dict[str, Any] = {}
    effort = ctx.resolved_effort
    if effort and not thinking_disabled and not spec.extended_thinking_only:
        if spec.effort_levels is not None:
            claude_kwargs["effort"] = _clamp_effort(effort, spec.effort_levels, target=ctx.model)
        else:
            logger.warning(
                f"{ctx.model} does not support reasoning effort; "
                f"AVA_REASONING_EFFORT={effort!r} ignored"
            )

    # Extended-thinking-only models (haiku-4-5) map budget_tokens / effort
    # onto their thinking on/off binary.
    extended_thinking = claude_extended_thinking_kwarg(
        ctx.model,
        thinking=ctx.thinking,
        budget_tokens=resolve_setting("claude_thinking_budget_tokens", model=ctx.model),
        reasoning_effort=effort,
    )
    if extended_thinking is not None:
        extra_kwargs["thinking"] = extended_thinking
    elif not thinking_disabled and not spec.extended_thinking_only:
        # Adaptive-thinking claude models (everything on the current API
        # except the extended-thinking-only ones) default to
        # `thinking.display="omitted"` server-side: the model thinks, but
        # the wire returns only a signature with no thinking text — the
        # stream emits no thinking_delta and the committed message carries
        # an empty thinking block, so the timeline has nothing to render.
        # Opt into summarized thinking text explicitly; a caller-passed
        # config keeps its own `display` when it set one.
        wire_thinking: dict[str, Any] = (
            dict(ctx.thinking) if ctx.thinking is not None else {"type": "adaptive"}
        )
        wire_thinking.setdefault("display", "summarized")
        extra_kwargs["thinking"] = wire_thinking

    # ThinkingTokensChatAnthropic subclasses ChatAnthropic to also surface
    # thinking_tokens in usage_metadata.output_token_details (the base
    # _create_usage_metadata drops them). Same __init__ signature.
    # cache_control enables Anthropic prompt caching — the system prompt and
    # eligible message blocks get cached server-side for 5 minutes (default TTL),
    # reducing input token cost and latency on repeated turns.
    return ThinkingTokensChatAnthropic(
        model=ctx.model,  # type: ignore[call-arg]
        api_key=api_key,
        max_tokens=spec.max_output_tokens,  # type: ignore[call-arg]
        model_kwargs={"cache_control": {"type": "ephemeral"}},
        timeout=ctx.timeout,
        **claude_kwargs,
        **extra_kwargs,
    )


register(
    ProviderBinding(
        prefix="claude-",
        display_name="Anthropic",
        key_env="ANTHROPIC_API_KEY",
        build=build,
        effort_levels=None,
        vision=True,
        anthropic_protocol=True,
        attach=AttachPolicy(
            file_size_limits={"image": 10 * 1024 * 1024, "pdf": 32 * 1024 * 1024},
            image_dimension_tiers=((1, 8000),),
            pdf_document_block=True,
        ),
        stop_spec=StopSpec(
            "anthropic",
            "stop_reason",
            frozenset({"end_turn", "tool_use", "refusal"}),
            frozenset({"max_tokens"}),
        ),
    ),
    models={
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
        "claude-opus-4-8": ModelSpec(
            provider="claude",
            context_window=200_000,
            max_output_tokens=128_000,
            knowledge_cutoff="2026-01",
            effort_levels=_CLAUDE_ADAPTIVE_EFFORT,
            tuning=ModelTuning(
                # User decision (2026-09-03): Claude family defaults this section off.
                prompt_user_tone_enabled=False,
            ),
            media_types=frozenset({"image", "pdf"}),
        ),
        "claude-sonnet-4-6": ModelSpec(
            provider="claude",
            context_window=200_000,
            max_output_tokens=128_000,
            knowledge_cutoff="2025-08",
            effort_levels=("low", "medium", "high", "max"),  # xhigh arrived with opus-4-7
            tuning=ModelTuning(
                # User decision (2026-09-03): Claude family defaults this section off.
                prompt_user_tone_enabled=False,
            ),
            media_types=frozenset({"image", "pdf"}),
        ),
        "claude-opus-4-7": ModelSpec(
            provider="claude",
            max_output_tokens=128_000,
            effort_levels=_CLAUDE_ADAPTIVE_EFFORT,
            tuning=ModelTuning(
                # User decision (2026-09-03): Claude family defaults this section off.
                prompt_user_tone_enabled=False,
            ),
            media_types=frozenset({"image", "pdf"}),
        ),
        "claude-opus-4-6": ModelSpec(
            provider="claude",
            tuning=ModelTuning(
                # User decision (2026-09-03): Claude family defaults this section off.
                prompt_user_tone_enabled=False,
            ),
            media_types=frozenset({"image", "pdf"}),
        ),
        # Bare alias of the dated snapshot above, kept for old agent configs.
        # Carries the same extended-thinking-only flag as the dated entry: without
        # it the factory's adaptive-thinking default would send `type: "adaptive"`,
        # which this model 400s on.
        "claude-haiku-4-5": ModelSpec(
            provider="claude",
            extended_thinking_only=True,
            tuning=ModelTuning(
                # User decision (2026-09-03): Claude family defaults this section off.
                prompt_user_tone_enabled=False,
            ),
            media_types=frozenset({"image", "pdf"}),
        ),
    },
    pricing={
        "claude-sonnet-5": PriceRates(
            cache_miss=2.0,
            cache_hit=0.20,
            output=10.0,
            source_url="https://www.anthropic.com/pricing",
            source_checked_at="2026-06-27",
            vendor="anthropic",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="2.0",
                            cache_hit="0.20",
                            output="10.0",
                        ),
                    ),
                ),
            ),
        ),
        "claude-haiku-4-5-20251001": PriceRates(
            cache_miss=1.0,
            cache_hit=0.10,
            output=5.0,
            source_url="https://www.anthropic.com/pricing",
            source_checked_at="2026-06-27",
            vendor="anthropic",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="1.0",
                            cache_hit="0.10",
                            output="5.0",
                        ),
                    ),
                ),
            ),
        ),
        "claude-opus-5": PriceRates(
            cache_miss=5.0,
            cache_hit=0.50,
            output=25.0,
            source_url="https://www.anthropic.com/pricing",
            source_checked_at="2026-06-27",
            vendor="anthropic",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="5.0",
                            cache_hit="0.50",
                            output="25.0",
                        ),
                    ),
                ),
            ),
        ),
        "claude-fable-5": PriceRates(
            cache_miss=10.0,
            cache_hit=1.0,
            output=50.0,
            source_url="https://www.anthropic.com/pricing",
            source_checked_at="2026-06-27",
            vendor="anthropic",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="10.0",
                            cache_hit="1.0",
                            output="50.0",
                        ),
                    ),
                ),
            ),
        ),
        "claude-fable-5-1": PriceRates(
            cache_miss=10.0,
            cache_hit=0.25,
            output=50.0,
            source_url="https://www.anthropic.com/pricing",
            source_checked_at="2026-09-02",
            vendor="anthropic",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="10.0",
                            cache_hit="0.25",
                            output="50.0",
                        ),
                    ),
                ),
            ),
        ),
        "claude-opus-4-8": PriceRates(
            cache_miss=5.0,
            cache_hit=0.50,
            output=25.0,
            source_url="https://www.anthropic.com/pricing",
            source_checked_at="2026-06-27",
            vendor="anthropic",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="5.0",
                            cache_hit="0.50",
                            output="25.0",
                        ),
                    ),
                ),
            ),
        ),
        "claude-sonnet-4-6": PriceRates(
            cache_miss=3.0,
            cache_hit=0.30,
            output=15.0,
            source_url="https://www.anthropic.com/pricing",
            source_checked_at="2026-06-27",
            vendor="anthropic",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="3.0",
                            cache_hit="0.30",
                            output="15.0",
                        ),
                    ),
                ),
            ),
        ),
        "claude-opus-4-7": PriceRates(
            cache_miss=5.0,
            cache_hit=0.50,
            output=25.0,
            source_url="https://www.anthropic.com/pricing",
            source_checked_at="2026-06-27",
            vendor="anthropic",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="5.0",
                            cache_hit="0.50",
                            output="25.0",
                        ),
                    ),
                ),
            ),
        ),
        "claude-opus-4-6": PriceRates(
            cache_miss=5.0,
            cache_hit=0.50,
            output=25.0,
            source_url="https://www.anthropic.com/pricing",
            source_checked_at="2026-06-27",
            vendor="anthropic",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="5.0",
                            cache_hit="0.50",
                            output="25.0",
                        ),
                    ),
                ),
            ),
        ),
        "claude-haiku-4-5": PriceRates(
            cache_miss=1.0,
            cache_hit=0.10,
            output=5.0,
            source_url="https://www.anthropic.com/pricing",
            source_checked_at="2026-06-27",
            vendor="anthropic",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="1.0",
                            cache_hit="0.10",
                            output="5.0",
                        ),
                    ),
                ),
            ),
        ),
    },
)
