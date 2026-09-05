"""Moonshot Kimi provider plugin for Ava.

ChatMoonshot captures streamed reasoning in
``additional_kwargs["reasoning_content"]`` rather than canonical content
blocks. The streaming fan-out and timeline both consume that shape.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from loguru import logger

from shared.lm._effort import _clamp_effort
from shared.lm.provider_api import (
    BuildContext,
    PriceRates,
    ProviderBinding,
    register,
    require_key,
)
from shared.lm.registry import ModelSpec, ModelTuning
from shared.lm.stop import StopSpec

_KIMI_EFFORT_LEVELS = ("low", "high", "max")


def build(ctx: BuildContext) -> BaseChatModel:
    """kimi-* branch: ChatMoonshot (OpenAI-compatible). K3's thinking is
    always on — a caller disabling it gets a warning. Reasoning effort
    rides the top-level `reasoning_effort` body field via extra_body."""
    from langchain_moonshot import ChatMoonshot

    # Moonshot Kimi API is OpenAI-compatible (https://api.moonshot.ai/v1),
    # standard `Authorization: Bearer` auth. K2.7 thinking is on by default,
    # streamed in the delta's `reasoning_content` field. ChatMoonshot captures
    # reasoning in `additional_kwargs["reasoning_content"]`; the streaming
    # fan-out (`RedisStreamHandler`) and timeline (`shared/timeline.py`) both
    # read that key so reasoning renders through the same path as every other
    # provider.
    api_key = require_key("MOONSHOT_API_KEY")
    # K3's thinking is always on and cannot be turned off, and the
    # K2.x-era `thinking` request parameter is not accepted by K3 — warn
    # instead of silently dropping the caller's intent. (Do NOT wire
    # ChatMoonshot's `thinking` field: it emits that K2.x parameter.)
    if ctx.thinking is not None and ctx.thinking.get("type") == "disabled":
        logger.warning(
            f"{ctx.model} cannot disable thinking; thinking={{'type': 'disabled'}} ignored"
        )
    # Reasoning effort rides the top-level `reasoning_effort` body field,
    # delivered via extra_body (Kimi's enum is low/high/max, default max —
    # the only way to run K3 below its most expensive tier).
    kimi_kwargs: dict[str, Any] = {}
    if ctx.resolved_effort:
        kimi_kwargs["extra_body"] = {
            "reasoning_effort": _clamp_effort(
                ctx.resolved_effort,
                ctx.effort_levels if ctx.effort_levels is not None else _KIMI_EFFORT_LEVELS,
                target="kimi",
            )
        }
    return ChatMoonshot(
        model=ctx.model,  # type: ignore[call-arg]
        api_key=api_key,  # type: ignore[arg-type]
        stream_usage=True,
        disable_streaming=ctx.disable_streaming,
        timeout=ctx.timeout,
        **kimi_kwargs,
    )


register(
    ProviderBinding(
        prefix="kimi-",
        display_name="Moonshot",
        key_env="MOONSHOT_API_KEY",
        build=build,
        effort_levels=_KIMI_EFFORT_LEVELS,
        vision=True,
        stop_spec=StopSpec(
            "moonshot",
            "finish_reason",
            frozenset({"stop", "tool_calls", "function_call"}),
            frozenset({"length"}),
        ),
    ),
    models={
        "kimi-k3": ModelSpec(
            provider="kimi",
            spawnable=True,
            context_window=1_048_576,
            knowledge_cutoff="2025-12",
            model_identity="You are running on Kimi K3 (Moonshot).",
            effort_levels=("low", "high", "max"),
            # Streams (the registry default). The former streaming=False carried
            # "streaming returns ~40% 429" — no incident record backs the asymmetry,
            # and Moonshot's own troubleshooting page recommends stream=True
            # precisely to avoid connection errors (non-streaming makes the server
            # withhold the response header until generation completes). The
            # asymmetry is more likely an artifact of the timeouts this commit
            # fixes: the streaming path was killed by a 30s TTFT while the
            # non-streaming fallback got 600s. See
            # decisions/2026-07-25-per-model-tuning-values.md.
            tuning=ModelTuning(
                # Pinned 2026-08-01 (task #568): Moonshot documents K3's default
                # effort as `max` (decisions/2026-07-25-per-model-tuning-
                # values.md Decision 4).
                reasoning_effort="max",
                # Moonshot's rate limits are account-wide across every key AND every
                # model, capacity for K3 was scarce enough that new subscriptions
                # were paused, and engine_overloaded_error is explicitly defined as
                # server-side capacity that upgrading your tier does not fix.
                llm_retry_max_attempts=10,
                # The recorded K3 failure (PR #496): the provider SDK retries a 429
                # internally, gets a 200, but the overloaded engine never starts
                # streaming — no bytes, so 30s TTFT fired before the retry could land.
                llm_stream_ttft_timeout_seconds=120.0,
            ),
            media_types=frozenset({"image"}),
        ),
    },
    pricing={
        "kimi-k3": PriceRates(
            cache_miss=3.0,
            cache_hit=0.30,
            output=15.0,
            source_url="https://platform.moonshot.ai/docs/pricing",
            source_checked_at="2026-07-25",
            vendor="moonshot",
        ),
    },
)
