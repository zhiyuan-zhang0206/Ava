# ava:ref ava_builtins/plugins/docs/plugins.ava.okf.md#lm-google-provider
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
from loguru import logger

from base.lm.compat.stop import StopSpec
from base.lm.effort import validate_effort
from base.lm.provider_api import (
    BuildContext,
    PricePeriod,
    PriceRates,
    PriceTier,
    ProviderBinding,
    ProviderContribution,
    require_key,
)
from base.lm.registry import ModelSpec, ModelTuning
from base.packages.plugins.extensions import PluginContributions

_GEMINI_EFFORT_LEVELS = ("minimal", "low", "medium", "high")


def _thinking_level(ctx: BuildContext, *, thinking_disabled: bool) -> str | None:
    thinking_level: str | None = None
    if ctx.media_thinking_level is not None:
        return ctx.media_thinking_level
    if thinking_disabled:
        spec = ctx.spec
        if spec is not None and spec.effort_levels is not None:
            thinking_level = spec.effort_levels[0] if spec.effort_levels else "minimal"
        else:
            logger.warning(
                f"{ctx.model} does not support the thinking_level vocabulary; "
                f"thinking={{'type': 'disabled'}} ignored (issue #190)"
            )
    if ctx.resolved_effort and not thinking_disabled:
        spec = ctx.spec
        levels = (
            spec.effort_levels
            if spec is not None and spec.effort_levels
            else (ctx.effort_levels if ctx.effort_levels is not None else _GEMINI_EFFORT_LEVELS)
        )
        thinking_level = validate_effort(
            ctx.resolved_effort,
            levels,
            target="gemini",
        )
    return thinking_level


def _media_resolution(setting: str | None) -> Any | None:
    if setting is None:
        return None
    from google.genai.types import MediaResolution

    resolutions = {
        "low": MediaResolution.MEDIA_RESOLUTION_LOW,
        "medium": MediaResolution.MEDIA_RESOLUTION_MEDIUM,
        "high": MediaResolution.MEDIA_RESOLUTION_HIGH,
    }
    try:
        return resolutions[setting]
    except KeyError:
        raise ValueError(
            f"media_resolution must be one of {sorted(resolutions)}, got {setting!r}"
        ) from None


def build(ctx: BuildContext) -> BaseChatModel:
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(**chat_model_kwargs(ctx, api_key=require_key("GEMINI_API_KEY")))


def chat_model_kwargs(ctx: BuildContext, *, api_key: str) -> dict[str, Any]:
    thinking_disabled = ctx.thinking is not None and ctx.thinking.get("type") == "disabled"
    thinking_level = _thinking_level(ctx, thinking_disabled=thinking_disabled)
    if ctx.media_thinking_level is not None:
        include_thoughts: bool | None = None
    elif thinking_disabled and thinking_level is None:
        include_thoughts = None
    else:
        include_thoughts = not thinking_disabled
    media_resolution_value = _media_resolution(ctx.media_resolution)
    kwargs: dict[str, Any] = {
        "model": ctx.model,
        "google_api_key": api_key,
        "include_thoughts": include_thoughts,
        "thinking_level": thinking_level,
        "disable_streaming": ctx.disable_streaming,
        "timeout": ctx.timeout,
        "temperature": None,
    }
    if media_resolution_value is not None:
        kwargs["media_resolution"] = media_resolution_value
    if ctx.base_url is not None:
        kwargs["base_url"] = ctx.base_url
    return kwargs


PROVIDER = ProviderContribution(
    binding=ProviderBinding(
        prefix="gemini-",
        display_name="Google",
        key_env="GEMINI_API_KEY",
        build=build,
        effort_levels=_GEMINI_EFFORT_LEVELS,
        vision=True,
        stop_spec=StopSpec(
            "google_genai",
            "finish_reason",
            frozenset({"STOP"}),
            frozenset({"MAX_TOKENS"}),
        ),
    ),
    models={
        "gemini-3.8-flash": ModelSpec(
            provider="gemini",
            spawnable=True,
            context_window=1_048_576,
            knowledge_cutoff="2026-03",
            effort_levels=("low", "medium", "high"),
            tuning=ModelTuning(
                reasoning_effort="medium",
            ),
            media_types=frozenset({"image", "pdf", "audio", "video"}),
        ),
        "gemini-3.7-flash": ModelSpec(
            provider="gemini",
            spawnable=True,
            superseded_by="gemini-3.8-flash",
            context_window=1_048_576,
            knowledge_cutoff="2026-03",
            effort_levels=("minimal", "low", "medium", "high"),
            tuning=ModelTuning(reasoning_effort="medium"),
            media_types=frozenset({"image", "pdf", "audio", "video"}),
        ),
        "gemini-3.5-flash": ModelSpec(
            provider="gemini",
            spawnable=True,
            superseded_by="gemini-3.8-flash",
            context_window=1_048_576,
            knowledge_cutoff="2025-01",
            effort_levels=("minimal", "low", "medium", "high"),
            tuning=ModelTuning(
                reasoning_effort="medium",
            ),
            media_types=frozenset({"image", "pdf", "audio", "video"}),
        ),
        "gemini-flash-lite-latest": ModelSpec(
            provider="gemini",
            spawnable=True,
            context_window=1_048_576,
            knowledge_cutoff="2026-03",
            effort_levels=("minimal", "low", "medium", "high"),
            tuning=ModelTuning(reasoning_effort="minimal"),
            media_types=frozenset({"image", "pdf", "audio", "video"}),
        ),
        "gemini-3.1-pro-preview": ModelSpec(
            provider="gemini",
            spawnable=True,
            context_window=1_048_576,
            knowledge_cutoff="2025-01",
            effort_levels=("low", "medium", "high"),
            tuning=ModelTuning(
                reasoning_effort="high",
                llm_retry_max_attempts=10,
                llm_stream_ttft_timeout_seconds=90.0,
            ),
            media_types=frozenset({"image", "pdf", "audio", "video"}),
        ),
        "gemini-4-argon": ModelSpec(
            provider="gemini",
            spawnable=True,
            context_window=1_048_576,
            knowledge_cutoff="2026-03",
            effort_levels=("low", "medium", "high"),
            tuning=ModelTuning(
                reasoning_effort="medium",
            ),
            media_types=frozenset({"image", "pdf", "audio", "video"}),
        ),
    },
    pricing={
        "gemini-3.8-flash": PriceRates(
            cache_miss=0.75,
            cache_hit=0.075,
            output=3.75,
            source_url="https://ai.google.dev/gemini-api/docs/pricing",
            source_checked_at="2026-09-03",
            vendor="google",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until="2026-12-31T10:00:00Z",
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="0.75",
                            cache_hit="0.075",
                            output="3.75",
                        ),
                    ),
                ),
                PricePeriod(
                    effective_from="2026-12-31T10:00:00Z",
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="1.50",
                            cache_hit="0.15",
                            output="7.50",
                        ),
                    ),
                ),
            ),
        ),
        "gemini-3.7-flash": PriceRates(
            cache_miss=0.75,
            cache_hit=0.075,
            output=3.75,
            source_url="https://ai.google.dev/gemini-api/docs/pricing",
            source_checked_at="2026-08-18",
            vendor="google",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until="2026-12-31T10:00:00Z",
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="0.75",
                            cache_hit="0.075",
                            output="3.75",
                        ),
                    ),
                ),
                PricePeriod(
                    effective_from="2026-12-31T10:00:00Z",
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="1.50",
                            cache_hit="0.15",
                            output="7.50",
                        ),
                    ),
                ),
            ),
        ),
        "gemini-3.5-flash": PriceRates(
            cache_miss=1.5,
            cache_hit=0.15,
            output=9.0,
            source_url="https://ai.google.dev/gemini-api/docs/pricing",
            source_checked_at="2026-06-27",
            vendor="google",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="1.5",
                            cache_hit="0.15",
                            output="9.0",
                        ),
                    ),
                ),
            ),
        ),
        "gemini-flash-lite-latest": PriceRates(
            cache_miss=0.30,
            cache_hit=0.03,
            output=2.50,
            source_url="https://ai.google.dev/gemini-api/docs/pricing",
            source_checked_at="2026-09-10",
            vendor="google",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="0.30",
                            cache_hit="0.03",
                            output="2.50",
                        ),
                    ),
                ),
            ),
        ),
        "gemini-3.1-pro-preview": PriceRates(
            cache_miss=2.0,
            cache_hit=0.2,
            output=12.0,
            source_url="https://ai.google.dev/gemini-api/docs/pricing",
            source_checked_at="2026-06-27",
            vendor="google",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=200000,
                            cache_miss="2.0",
                            cache_hit="0.2",
                            output="12.0",
                        ),
                        PriceTier(
                            input_tokens_min=200001,
                            input_tokens_max=None,
                            cache_miss="4.0",
                            cache_hit="0.4",
                            output="18.0",
                        ),
                    ),
                ),
            ),
        ),
        "gemini-4-argon": PriceRates(
            cache_miss=2.0,
            cache_hit=0.10,
            output=10.0,
            source_url="https://ai.google.dev/gemini-api/docs/pricing",
            source_checked_at="2026-10-14",
            vendor="google",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until="2026-11-30T10:00:00Z",
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="2.0",
                            cache_hit="0.10",
                            output="10.0",
                        ),
                    ),
                ),
                PricePeriod(
                    effective_from="2026-11-30T10:00:00Z",
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="4.0",
                            cache_hit="0.20",
                            output="20.0",
                        ),
                    ),
                ),
            ),
        ),
    },
)


def contribute() -> PluginContributions:
    return PluginContributions(providers=(PROVIDER,))
