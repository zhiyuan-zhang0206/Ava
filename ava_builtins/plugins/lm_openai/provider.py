"""OpenAI provider plugin for Ava."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from shared.lm.provider_api import (
    BuildContext,
    PriceRates,
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
    gpt_effort = ctx.resolved_effort or "medium"
    gpt_reasoning: dict[str, Any] = (
        {"effort": "none"} if thinking_disabled else {"effort": gpt_effort, "summary": "auto"}
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
        "gpt-5.6-sol": PriceRates(
            cache_miss=5.0,
            cache_hit=0.5,
            output=30.0,
            source_url="https://openai.com/api/pricing/",
            source_checked_at="2026-06-27",
            vendor="openai",
        ),
        "gpt-5.6-terra": PriceRates(
            cache_miss=2.0,
            cache_hit=0.20,
            output=12.0,
            source_url="https://developers.openai.com/api/docs/models/gpt-5.6-terra",
            source_checked_at="2026-07-30",
            vendor="openai",
        ),
        "gpt-5.6-luna": PriceRates(
            cache_miss=0.20,
            cache_hit=0.02,
            output=1.20,
            source_url="https://developers.openai.com/api/docs/models/gpt-5.6-luna",
            source_checked_at="2026-07-30",
            vendor="openai",
        ),
        "gpt-5.5": PriceRates(
            cache_miss=5.0,
            cache_hit=0.5,
            output=30.0,
            source_url="https://openai.com/api/pricing/",
            source_checked_at="2026-06-27",
            vendor="openai",
        ),
        "gpt-5.4-mini": PriceRates(
            cache_miss=0.75,
            cache_hit=0.075,
            output=4.5,
            source_url="https://openai.com/api/pricing/",
            source_checked_at="2026-06-27",
            vendor="openai",
        ),
    },
)
