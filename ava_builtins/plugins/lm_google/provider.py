"""Google Gemini provider plugin for Ava."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Annotation-only (`build` return): keeps the chat-model stack off the
    # provider-registration path (exec-child boot, task #3633;
    # `_TYPE_CHECKING_ALLOWED`).
    from langchain_core.language_models.chat_models import BaseChatModel
from loguru import logger

from base.lm.call import LlmInvocation, ProviderCallContext
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
from base.lm.stop import StopSpec
from base.packages.plugins.extensions import PluginContributions

_GEMINI_EFFORT_LEVELS = ("minimal", "low", "medium", "high")


def _thinking_level(ctx: BuildContext, *, thinking_disabled: bool) -> str | None:
    """The `thinking_level` for this request, by path: media knob, disabled, or resolved effort."""
    thinking_level: str | None = None
    if ctx.media_thinking_level is not None:
        # Media path — the caller owns the Gemini vocabulary mapping
        # (ava/understand.py), including the `max` → configured-knob
        # special case; the resolved-effort path is skipped entirely so a
        # global AVA_REASONING_EFFORT cannot silently override the media
        # knob (historic behavior).
        return ctx.media_thinking_level
    if thinking_disabled:
        # thinking_level is a Gemini 3.x vocabulary; older models
        # (gemini-2.5-*) reject it with a 400 on every call (issue #190), so
        # "disabled" is only expressible where the model declares the
        # vocabulary. Elsewhere it is a no-op — no thinking parameters on the
        # wire, exactly what the issue measured as working — plus a warning
        # so the operator knows the request was not honored.
        spec = ctx.spec
        if spec is not None and spec.effort_levels is not None:
            thinking_level = spec.effort_levels[0] if spec.effort_levels else "minimal"
        else:
            logger.warning(
                f"{ctx.model} does not support the thinking_level vocabulary; "
                f"thinking={{'type': 'disabled'}} ignored (issue #190)"
            )
    if ctx.resolved_effort and not thinking_disabled:
        # A declared model vocabulary is authoritative: 3.8 Flash does not
        # accept `minimal`, even though the Gemini-wide fallback includes it.
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
    """Map the low/medium/high media-resolution setting onto Google's MediaResolution enum."""
    # Media-resolution mapping (ava.understand's media path). The enum import
    # is lazy like the model class itself so the google-genai SDK binding
    # stays inside this provider branch.
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
    """gemini-* branch: ChatGoogleGenerativeAI. include_thoughts surfaces
    the thinking summary as canonical thinking blocks; thinking depth
    rides thinking_level (cannot fully turn off — disabled maps to
    "minimal" + include_thoughts=False).

    Media-path extras (ava.understand): `media_resolution` maps the
    low/medium/high setting onto Google's MediaResolution enum;
    `media_thinking_level` carries the Gemini thinking_level vocabulary
    explicitly (the media path maps effort itself, including the
    `max` → configured-knob special case) and keeps include_thoughts at
    the SDK default — the media path never surfaced thought blocks, and
    surfacing them would change latency/cost even though the response
    flattener drops them. `base_url` overrides the Gemini endpoint.
    All three are None on the agent main path, so it behaves exactly as
    before."""
    from langchain_google_genai import ChatGoogleGenerativeAI

    # Reuse GEMINI_API_KEY (already used by ava.understand). Direct
    # construction matches the claude-/deepseek- branches; the repo has
    # no `langchain` meta-package, so no init_chat_model dispatcher.
    # Fail fast on missing key rather than a later server 401.
    api_key = require_key("GEMINI_API_KEY")
    # include_thoughts surfaces the model's thinking summary as
    # `{"type":"thinking", "thinking":...}` content blocks — the same
    # shape claude/deepseek emit, so the streaming fan-out and timeline
    # render reasoning without a provider-specific branch. Without it the
    # model still thinks but returns zero thought blocks (no reasoning in
    # the live view).
    #
    # Thinking depth rides `thinking_level`. Gemini 3.x cannot fully turn
    # thinking off, so a caller disabling thinking (short-text paths) gets
    # the model's lowest declared level + include_thoughts=False — dropping
    # include_thoughts alone would only hide the thought blocks while the
    # model keeps thinking (and billing) at its default level. Otherwise
    # the cross-provider effort maps onto the thinking_level vocabulary;
    # unset effort leaves thinking_level None → the model default
    # (3.8/3.5 Flash medium, 3.1 Pro high).
    thinking_disabled = ctx.thinking is not None and ctx.thinking.get("type") == "disabled"
    thinking_level = _thinking_level(ctx, thinking_disabled=thinking_disabled)
    # include_thoughts only on the vocabulary-supporting path: passing it to a
    # model that rejects thinking parameters would 400 exactly like
    # thinking_level (issue #190). Unsupported models keep the SDK default;
    # the non-disabled path keeps its historical include_thoughts=True.
    if ctx.media_thinking_level is not None:
        # Media path historic behavior: the field was never set on the
        # ChatGoogleGenerativeAI constructor, so leave it at the SDK default
        # (None) — do not surface thought blocks.
        include_thoughts: bool | None = None
    elif thinking_disabled and thinking_level is None:
        include_thoughts = None
    else:
        include_thoughts = not thinking_disabled
    media_resolution_value = _media_resolution(ctx.media_resolution)
    kwargs: dict[str, Any] = {
        "model": ctx.model,  # type: ignore[call-arg]
        "google_api_key": api_key,
        "include_thoughts": include_thoughts,
        "thinking_level": thinking_level,  # type: ignore[arg-type]
        "disable_streaming": ctx.disable_streaming,
        "timeout": ctx.timeout,
    }
    if media_resolution_value is not None:
        kwargs["media_resolution"] = media_resolution_value
    if ctx.base_url is not None:
        kwargs["base_url"] = ctx.base_url
    return ChatGoogleGenerativeAI(**kwargs)


async def prepare_call(ctx: ProviderCallContext) -> LlmInvocation | None:
    """Load the Google invocation adapter only when this binding makes a call."""
    from ava_builtins.plugins.lm_google.gemini_cache import prepare_call as prepare

    return await prepare(ctx)


PROVIDER = ProviderContribution(
    binding=ProviderBinding(
        prefix="gemini-",
        display_name="Google",
        key_env="GEMINI_API_KEY",
        build=build,
        prepare_call=prepare_call,
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
            # Google does not publish a cutoff for 3.8 Flash; carries the 3.7
            # estimate forward (3.7 GA'd 2026-08-13, 3.8 GA'd 2026-09-02).
            knowledge_cutoff="2026-03",
            # thinking_level vocabulary; `minimal` 400s (verified live 2026-09-03).
            effort_levels=("low", "medium", "high"),
            tuning=ModelTuning(
                # The model page says its default thinking_level is `medium`
                # (docs/decisions/engineering/design/simplification/2026-07-25-per-model-tuning-values.md).
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
        "gemini-flash-lite-latest": ModelSpec(
            provider="gemini",
            spawnable=True,
            # The registered id is Google's official `latest` alias, which
            # tracks the newest stable Flash-Lite; it resolves to
            # gemini-3.5-flash-lite (GA 2026-07; ai.google.dev models page,
            # checked 2026-09-10). The user's dev channel verified the alias
            # answers while the concrete gemini-2.5-flash-lite id 404s. Facts
            # below are the 3.5 Flash-Lite ones; re-verify them when the
            # alias moves to a newer Flash-Lite.
            context_window=1_048_576,
            knowledge_cutoff="2026-03",
            # The Gemini thinking guide lists minimal/low/medium/high for
            # 3.5 Flash-Lite; its default thinking is `minimal` (the flash
            # models default to `medium`).
            effort_levels=("minimal", "low", "medium", "high"),
            tuning=ModelTuning(reasoning_effort="minimal"),
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
                # also recorded in docs/decisions/2026-07-25-per-model-tuning-
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
        # Removed 2026-09-23 (task #4508): gemini-2.5-pro and gemini-2.5-flash
        # were never selectable here (implicit spawnable=False) and have no
        # live references. Historical prices remain in
        # base/lm/pricing/pricing_catalog_archive.json.
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
    },
)


def contribute() -> PluginContributions:
    """What this plugin declares: its model provider."""
    return PluginContributions(providers=(PROVIDER,))
