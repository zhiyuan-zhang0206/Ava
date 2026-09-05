"""Zhipu GLM provider plugin for Ava."""

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

_GLM_EFFORT_LEVELS = ("low", "high", "max")


def build(ctx: BuildContext) -> BaseChatModel:
    """glm-* branch: ReasoningContentChatModel (OpenAI-compatible).
    Thinking off rides top-level `thinking` via extra_body; otherwise
    reasoning effort rides the `reasoning_effort` constructor field.
    Models whose reasoning is always on (glm-5.3 / glm-5.3-flash,
    `ModelSpec.thinking_always_on`) get a warning instead — their endpoint
    rejects thinking.type=disabled with a 400 (error code 1210), so sending
    the body would fail the call rather than honor the intent."""
    from shared.lm._reasoning_compat import ReasoningContentChatModel

    # Zhipu GLM API is OpenAI-compatible (https://open.bigmodel.cn/api/paas/v4),
    # standard `Authorization: Bearer` auth. GLM 5.2 streams thinking in the
    # delta's `reasoning_content` field — same ReasoningContentChatModel recovers
    # it into canonical thinking blocks.
    api_key = require_key("GLM_API_KEY")
    # GLM natively supports turning thinking off — top-level `thinking` in
    # the POST body via extra_body. Disabled thinking skips effort
    # injection (deepseek-style: the caller asked for the cheap path).
    # Otherwise reasoning effort rides the OpenAI-standard
    # `reasoning_effort` payload field, a declared ChatOpenAI constructor
    # field (GLM's enum is low/high/max, default max — `low` and `high` are
    # the cheaper tiers; checked 2026-08-23 for GLM-5.3).
    glm_kwargs: dict[str, Any] = {}
    if ctx.thinking is not None and ctx.thinking.get("type") == "disabled":
        if ctx.spec is not None and ctx.spec.thinking_always_on:
            # glm-5.3 / glm-5.3-flash always think — the endpoint rejects
            # thinking.type=disabled (400, error code 1210, live-checked
            # 2026-08-27). Warn like the kimi branch instead of sending a
            # body that fails the call.
            logger.warning(
                f"{ctx.model} cannot disable thinking; thinking={{'type': 'disabled'}} ignored"
            )
        else:
            glm_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    elif ctx.resolved_effort:
        glm_kwargs["reasoning_effort"] = _clamp_effort(
            ctx.resolved_effort,
            ctx.effort_levels if ctx.effort_levels is not None else _GLM_EFFORT_LEVELS,
            target="glm",
        )
    return ReasoningContentChatModel(
        model=ctx.model,  # type: ignore[call-arg]
        api_key=api_key,  # type: ignore[arg-type]
        base_url="https://open.bigmodel.cn/api/paas/v4",
        stream_usage=True,
        disable_streaming=ctx.disable_streaming,
        timeout=ctx.timeout,
        **glm_kwargs,
    )


register(
    ProviderBinding(
        prefix="glm-",
        display_name="Zhipu",
        key_env="GLM_API_KEY",
        build=build,
        effort_levels=_GLM_EFFORT_LEVELS,
        vision=False,
        stop_spec=None,
    ),
    models={
        "glm-5.2": ModelSpec(
            provider="glm",
            spawnable=True,
            context_window=1_000_000,
            knowledge_cutoff="2025-12",
            # GLM-5.3 docs document the shared GLM-5-series parameter values
            # low/high/max (checked 2026-08-23); keep this entry aligned with the
            # provider clamp and its gateway invariant test.
            effort_levels=("low", "high", "max"),
            tuning=ModelTuning(
                # Pinned 2026-08-01 (task #568): Z.ai documents GLM-5.2's default
                # effort as `max` (decisions/2026-07-25-per-model-tuning-
                # values.md Decision 4).
                reasoning_effort="max",
                # The roster's best-documented overload history, from Z.ai's own
                # issue tracker: a single non-batch paid user logged 285 HTTP 429s in
                # one day (~50% of all requests) and a full hour at 100% failure.
                # Z.ai's own guidance for their 1305 code ("platform service overload") is to
                # lengthen the retry interval and avoid fixed-interval hammering.
                llm_retry_max_attempts=10,
            ),
        ),
        "glm-5.3": ModelSpec(
            provider="glm",
            spawnable=True,
            context_window=1_000_000,
            # Zhipu publishes no knowledge cutoff. GLM-5.3 shares GLM-5.2's base model
            # (release notes: all gains from post-training; checked 2026-08-23), so the
            # conservative glm-5.2 value carries over — erring early is the safe direction.
            knowledge_cutoff="2025-12",
            # docs.z.ai/guides/llm/glm-5.3: reasoning_effort low/high/max, default max.
            effort_levels=("low", "high", "max"),
            # docs.z.ai/guides/overview/migrate-to-glm-new + live check 2026-08-27:
            # GLM-5.3 always thinks — thinking.type=disabled is rejected with error
            # code 1210: "this model always thinks, disabling is unsupported"), so the
            # glm builder warns
            # instead of sending the disabled body (kimi-k3 pattern).
            thinking_always_on=True,
            tuning=ModelTuning(
                # Z.ai documents GLM-5.3's default effort as max.
                reasoning_effort="max",
                # Same GLM-family overload history rationale as glm-5.2's entry
                # (llm_retry_max_attempts=10).
                llm_retry_max_attempts=10,
            ),
        ),
        "glm-5.3-flash": ModelSpec(
            provider="glm",
            spawnable=True,
            # docs.z.ai/guides/vlm/glm-5.3-flash: 1M-token context window.
            context_window=1_000_000,
            # Zhipu publishes no knowledge cutoff for any GLM-5 model; carries the
            # glm-5.3 entry's conservative 2025-12 estimate forward — erring early
            # is the safe direction (see glm-5.3's entry).
            knowledge_cutoff="2025-12",
            # docs.z.ai/api-reference/llm/chat-completion: "For the GLM-5.3
            # GLM-5.3-FLASH model, only the low / high / max levels are supported"
            # (default max); verified live 2026-08-27 that low is accepted.
            effort_levels=("low", "high", "max"),
            # docs.z.ai/guides/vlm/glm-5.3-flash documents native multimodal input
            # (images, videos, files) — but the OpenAI-compatible binding Ava dials
            # renders image_url blocks only today, the same conservatism as
            # qwen3.8-max (whose official modality list also includes video).
            media_types=frozenset({"image"}),
            # Same always-on thinking as glm-5.3 (docs + live 400 on
            # thinking.type=disabled, error code 1210).
            thinking_always_on=True,
            tuning=ModelTuning(
                # Z.ai documents the GLM-5.3 series' default effort as max.
                reasoning_effort="max",
                # Same GLM-family overload history rationale as glm-5.2's entry
                # (llm_retry_max_attempts=10).
                llm_retry_max_attempts=10,
            ),
        ),
    },
    pricing={
        "glm-5.2": PriceRates(
            cache_miss=1.40,
            cache_hit=0.26,
            output=4.40,
            source_url="https://docs.z.ai/guides/overview/pricing",
            source_checked_at="2026-08-23",
            vendor="zhipu",
        ),
        "glm-5.3": PriceRates(
            cache_miss=1.40,
            cache_hit=0.26,
            output=4.40,
            source_url="https://docs.z.ai/guides/overview/pricing",
            source_checked_at="2026-08-23",
            vendor="zhipu",
        ),
        "glm-5.3-flash": PriceRates(
            cache_miss=0.075,
            cache_hit=0.015,
            output=0.25,
            source_url="https://docs.z.ai/guides/overview/pricing",
            source_checked_at="2026-08-27",
            vendor="zhipu",
        ),
    },
)
