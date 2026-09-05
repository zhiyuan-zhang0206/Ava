"""Xiaomi MiMo provider plugin for Ava."""

from __future__ import annotations

from collections.abc import Mapping
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

_MIMO_EFFORT_LEVELS = ("none", "high")


def mimo_extra_body(
    *,
    thinking: Mapping[str, Any] | None,
    reasoning_effort: str,
    effort_levels: tuple[str, ...],
) -> dict[str, Any]:
    """Resolve the mimo branch's `extra_body` kwarg.

    Caller-explicit `thinking={"type":"disabled"}` (short-text paths) wins
    outright. Otherwise `reasoning_effort` clamped onto the binding's effort
    vocabulary toggles the same body switch: "none" disables thinking,
    "high" is the provider default (already on — nothing to send). Empty dict
    = no override, provider default applies.
    """
    if thinking is not None and thinking.get("type") == "disabled":
        return {"thinking": {"type": "disabled"}}
    if reasoning_effort:
        tier = _clamp_effort(reasoning_effort, effort_levels, target="mimo")
        if tier == "none":
            return {"thinking": {"type": "disabled"}}
    return {}


def build(ctx: BuildContext) -> BaseChatModel:
    """mimo-* branch: ReasoningContentChatModel (OpenAI-compatible).
    Auth uses the `api-key` header; deep thinking on/off rides top-level
    `thinking` in the POST body via `mimo_extra_body`."""
    from shared.lm._reasoning_compat import ReasoningContentChatModel

    # Xiaomi MiMo API is OpenAI-compatible (https://api.xiaomimimo.com/v1).
    # Auth uses the `api-key` header (not the standard `Authorization: Bearer`).
    # Pass both api_key (Authorization: Bearer) and default_headers (api-key)
    # so the endpoint accepts either.
    # UltraSpeed mode (mimo-v2.5-pro-ultraspeed, ~1000 token/s) is the same
    # OpenAI-compatible call selected purely by model name. It is listed in
    # SUPPORTED_MODELS, but access is an application-gated, time-limited
    # trial — an unapproved account 400s with "Not supported model" (apply
    # at https://platform.xiaomimimo.com/ultraspeed).
    # ReasoningContentChatModel (not bare ChatOpenAI) recovers MiMo's
    # `reasoning_content` delta into canonical thinking blocks — the base drops it.
    api_key = require_key("MIMO_API_KEY")
    # MiMo natively supports switching deep thinking off — top-level
    # `thinking` in the POST body, delivered via extra_body (declared on
    # BaseChatOpenAI; model_kwargs would collide). No graded
    # reasoning_effort field on MiMo — see `mimo_extra_body` for the on/off
    # mapping.
    mimo_kwargs: dict[str, Any] = {}
    extra_body = mimo_extra_body(
        thinking=ctx.thinking,
        reasoning_effort=ctx.resolved_effort,
        effort_levels=(ctx.effort_levels if ctx.effort_levels is not None else _MIMO_EFFORT_LEVELS),
    )
    if extra_body:
        mimo_kwargs["extra_body"] = extra_body
    return ReasoningContentChatModel(
        model=ctx.model,  # type: ignore[call-arg]
        api_key=api_key,  # type: ignore[arg-type]
        base_url="https://api.xiaomimimo.com/v1",
        default_headers={"api-key": api_key},
        stream_usage=True,
        disable_streaming=ctx.disable_streaming,
        timeout=ctx.timeout,
        **mimo_kwargs,
    )


register(
    ProviderBinding(
        prefix="mimo-",
        display_name="Xiaomi",
        key_env="MIMO_API_KEY",
        build=build,
        effort_levels=_MIMO_EFFORT_LEVELS,
        vision=False,
        stop_spec=None,
    ),
    models={
        "mimo-v2.5-pro": ModelSpec(
            provider="mimo",
            spawnable=True,
            # Xiaomi's model page and the HF card both say 1M context / 128K max
            # output — the old 128,000 window was the OUTPUT cap filed as the
            # window, which understated the window ~8x and made the compact
            # threshold unreachable in practice.
            context_window=1_000_000,
            max_output_tokens=128_000,
            knowledge_cutoff="2024-12",
            effort_levels=("none", "high"),  # body-level thinking on/off only
            tuning=ModelTuning(
                # Pinned 2026-08-01 (task #568): the "high" rung IS the provider
                # default — thinking is on unless explicitly disabled
                # (`mimo_extra_body`).
                reasoning_effort="high",
            ),
        ),
        "mimo-v2.5-pro-ultraspeed": ModelSpec(
            provider="mimo",
            spawnable=True,
            # Same 1T/42B weights as Pro, served on the TileRT stack — same window
            # and output cap; only throughput and price differ.
            context_window=1_000_000,
            max_output_tokens=128_000,
            knowledge_cutoff="2024-12",
            effort_levels=("none", "high"),
            tuning=ModelTuning(
                # Pinned 2026-08-01 (task #568): "high" = provider default (see
                # mimo-v2.5-pro).
                reasoning_effort="high",
                # Xiaomi omits this variant from the published RPM/TPM table
                # entirely and gates it behind an application ("limited slots"),
                # i.e. its serving capacity is self-declared scarce.
                llm_retry_max_attempts=10,
            ),
        ),
    },
    pricing={
        "mimo-v2.5-pro": PriceRates(
            cache_miss=0.435,
            cache_hit=0.0036,
            output=0.87,
            source_url="https://platform.xiaomimimo.com",
            source_checked_at="2026-06-27",
            vendor="xiaomi",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="0.435",
                            cache_hit="0.0036",
                            output="0.87",
                        ),
                    ),
                ),
            ),
        ),
        "mimo-v2.5-pro-ultraspeed": PriceRates(
            cache_miss=1.305,
            cache_hit=0.0108,
            output=2.61,
            source_url="https://platform.xiaomimimo.com",
            source_checked_at="2026-06-27",
            vendor="xiaomi",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="1.305",
                            cache_hit="0.0108",
                            output="2.61",
                        ),
                    ),
                ),
            ),
        ),
    },
)
