"""OpenAI provider plugin for Ava."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from shared.lm._effort import _clamp_effort
from shared.lm.provider_api import (
    BuildContext,
    PricePeriod,
    PriceRates,
    PriceTier,
    ProviderBinding,
    register,
    require_key,
)
from shared.lm.registry import ModelSpec, ModelTuning
from shared.lm.stop import StopCategory, StopSpec

# Effort vocabulary shared by every GPT model — named once so the entries stay
# readable; it remains per-model data.
_GPT_EFFORT = ("none", "low", "medium", "high", "xhigh", "max")


def build(ctx: BuildContext) -> BaseChatModel:
    """gpt-* branch: ChatOpenAI on the Responses API. `reasoning.{effort,
    summary}` returns the reasoning summary as a canonical block; a
    caller disabling thinking drops to effort "none". (The Chat
    Completions path returns zero reasoning.)"""
    from langchain_openai import ChatOpenAI

    # Direct construction keeps this SDK-specific path explicit.
    api_key = require_key("OPENAI_API_KEY")
    # The Chat Completions path returns zero reasoning. The Responses API
    # with `reasoning.{effort,summary}` returns the model's reasoning
    # summary as a `{"type":"reasoning","summary":[{"type":"summary_text",
    # "text":...}]}` content block, streamed incrementally — the streaming
    # fan-out and timeline pull the visible text out of summary[].text
    # (folded to the canonical `thinking` shape by shared.lm.reasoning).
    #
    # effort must be set: the model's default reasoning effort is too low
    # to emit a summary, so `summary="auto"` alone yields zero reasoning
    # every turn (verified empirically). "medium" reliably surfaces a
    # summary; raw chain of thought stays provider-hidden, only the summary
    # is exposed. A caller disabling thinking (short-text paths) drops to
    # effort "none". Tool-call args still stream through `tool_call_chunks`,
    # so code rendering is unaffected by the API switch.
    thinking_disabled = ctx.thinking is not None and ctx.thinking.get("type") == "disabled"
    gpt_effort = "none" if thinking_disabled else (ctx.resolved_effort or "medium")
    # Clamp onto the model's declared effort vocabulary (per-model
    # ModelSpec.effort_levels). gpt-6 dropped "none" and "minimal" from the
    # wire vocabulary (official guide: start at "low" when coming from
    # either), so an explicit AVA_REASONING_EFFORT=none/minimal or a
    # thinking-disabled build must not reach the wire unclamped. Models
    # without a declared vocabulary (gpt-5.5, gpt-5.4-mini) keep the
    # historic verbatim passthrough.
    model_levels = ctx.spec.effort_levels if ctx.spec is not None else None
    if model_levels is not None:
        gpt_effort = _clamp_effort(gpt_effort, model_levels, target=ctx.model)
    gpt_reasoning: dict[str, Any] = (
        {"effort": gpt_effort} if thinking_disabled else {"effort": gpt_effort, "summary": "auto"}
    )
    return ChatOpenAI(
        model=ctx.model,  # type: ignore[call-arg]
        api_key=api_key,  # type: ignore[arg-type]
        use_responses_api=True,
        reasoning=gpt_reasoning,
        disable_streaming=ctx.disable_streaming,
        timeout=ctx.timeout,
    )


register(
    ProviderBinding(
        prefix="gpt-",
        display_name="OpenAI",
        key_env="OPENAI_API_KEY",
        build=build,
        effort_levels=_GPT_EFFORT,
        vision=True,
        stop_spec=StopSpec(
            "openai",
            "finish_reason",
            frozenset({"stop", "tool_calls", "function_call"}),
            frozenset({"length"}),
            status_key="status",
            status_map={
                "completed": StopCategory.NORMAL,
                "incomplete": StopCategory.TRUNCATED,
            },
        ),
    ),
    models={
        "gpt-6-astra": ModelSpec(
            provider="gpt",
            spawnable=True,
            context_window=1_050_000,
            knowledge_cutoff="2026-04",
            # gpt-6 dropped "none" and "minimal" from the effort vocabulary
            # (official model guide: start at "low" when coming from either);
            # the builder clamps out-of-vocabulary efforts onto these rungs.
            effort_levels=("low", "medium", "high", "xhigh", "max"),
            tuning=ModelTuning(
                reasoning_effort="medium",  # OpenAI default (see gpt-5.6-sol)
            ),
            media_types=frozenset({"image"}),
        ),
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
        "gpt-5.5": ModelSpec(
            provider="gpt",
            context_window=256_000,
            knowledge_cutoff="2025-12",
            media_types=frozenset({"image"}),
        ),
        "gpt-5.4-mini": ModelSpec(
            provider="gpt",
            context_window=256_000,
            knowledge_cutoff="2025-08",
            media_types=frozenset({"image"}),
        ),
    },
    pricing={
        "gpt-6-astra": PriceRates(
            cache_miss=10.0,
            cache_hit=1.0,
            output=50.0,
            source_url="https://developers.openai.com/api/docs/models/gpt-6-astra",
            source_checked_at="2026-09-06",
            vendor="openai",
            # Cache writes bill 1.25x uncached input ($12.50/M) and prompts
            # >272K input tokens bill 2x input+cache / 1.5x output for the
            # full request; both stay notes rather than tiers, matching the
            # flat single-tier convention of the gpt-5.6 entries (which carry
            # the same >272K rule unmodeled).
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
        "gpt-5.6-sol": PriceRates(
            cache_miss=5.0,
            cache_hit=0.5,
            output=30.0,
            source_url="https://openai.com/api/pricing/",
            source_checked_at="2026-06-27",
            vendor="openai",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="5.0",
                            cache_hit="0.5",
                            output="30.0",
                        ),
                    ),
                ),
            ),
        ),
        "gpt-5.6-terra": PriceRates(
            cache_miss=2.0,
            cache_hit=0.20,
            output=12.0,
            source_url="https://developers.openai.com/api/docs/models/gpt-5.6-terra",
            source_checked_at="2026-07-30",
            vendor="openai",
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
                            output="12.0",
                        ),
                    ),
                ),
            ),
        ),
        "gpt-5.6-luna": PriceRates(
            cache_miss=0.20,
            cache_hit=0.02,
            output=1.20,
            source_url="https://developers.openai.com/api/docs/models/gpt-5.6-luna",
            source_checked_at="2026-07-30",
            vendor="openai",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="0.20",
                            cache_hit="0.02",
                            output="1.20",
                        ),
                    ),
                ),
            ),
        ),
        "gpt-5.5": PriceRates(
            cache_miss=5.0,
            cache_hit=0.5,
            output=30.0,
            source_url="https://openai.com/api/pricing/",
            source_checked_at="2026-06-27",
            vendor="openai",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="5.0",
                            cache_hit="0.5",
                            output="30.0",
                        ),
                    ),
                ),
            ),
        ),
        "gpt-5.4-mini": PriceRates(
            cache_miss=0.75,
            cache_hit=0.075,
            output=4.5,
            source_url="https://openai.com/api/pricing/",
            source_checked_at="2026-06-27",
            vendor="openai",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="0.75",
                            cache_hit="0.075",
                            output="4.5",
                        ),
                    ),
                ),
            ),
        ),
    },
)
