"""Alibaba Qwen provider plugin for Ava."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from shared.config import settings
from shared.lm._effort import _clamp_effort
from shared.lm.provider_api import (
    BuildContext,
    PriceRates,
    ProviderBinding,
    register,
    require_key,
)
from shared.lm.registry import ModelSpec, ModelTuning

_QWEN_EFFORT_LEVELS = ("none", "high")


def qwen_extra_body(
    *,
    thinking: Mapping[str, Any] | None,
    reasoning_effort: str,
    effort_levels: tuple[str, ...],
) -> dict[str, Any]:
    """Resolve the qwen branch's `extra_body` kwarg.

    Same shape as `mimo_extra_body` on a different wire switch: DashScope's
    compatible-mode endpoint carries thinking on the top-level `enable_thinking`
    boolean. Caller-explicit `thinking={"type":"disabled"}` (short-text paths)
    wins outright; otherwise `reasoning_effort` clamped onto the binding's
    effort vocabulary toggles the same switch — "none" sends
    `enable_thinking=False`, "high" is the registered roster's own default
    (already on — nothing to send). Empty dict = no override.
    """
    if thinking is not None and thinking.get("type") == "disabled":
        return {"enable_thinking": False}
    if reasoning_effort:
        tier = _clamp_effort(reasoning_effort, effort_levels, target="qwen")
        if tier == "none":
            return {"enable_thinking": False}
    return {}


def build(ctx: BuildContext) -> BaseChatModel:
    """qwen* branch: ReasoningContentChatModel (OpenAI-compatible).
    Thinking on/off rides top-level `enable_thinking` via extra_body; there is
    no graded effort field on this endpoint (`qwen_extra_body`)."""
    from shared.lm._reasoning_compat import ReasoningContentChatModel

    # Alibaba Cloud Model Studio (DashScope) serves Qwen on an OpenAI-compatible
    # endpoint with standard `Authorization: Bearer` auth. The host is CONFIG,
    # not a constant: a dedicated Model Studio workspace serves the same API on
    # its own `<workspace-id>.cn-beijing.maas.aliyuncs.com` host, which the
    # public default cannot reach at all, so a hardcoded URL locks those accounts
    # out entirely (`AVA_DASHSCOPE_BASE_URL`). The `/compatible-mode/v1` suffix is
    # the load-bearing part — `/api/v1` on the same host is DashScope's native
    # protocol and 404s this client's paths. Regions price differently, so
    # repointing it means re-checking the PriceRates declarations below too.
    # Qwen streams its thinking in the delta's `reasoning_content` field,
    # which ReasoningContentChatModel recovers into canonical thinking blocks —
    # the same reason glm / mimo use it rather than bare ChatOpenAI.
    api_key = require_key("DASHSCOPE_API_KEY")
    # The registered Qwen roster thinks by default and can be switched off with
    # the body-level `enable_thinking` boolean, delivered via extra_body
    # (declared on BaseChatOpenAI; model_kwargs would collide). DashScope's
    # graded knob is a token budget (`thinking_budget`), not a level enum, so
    # the cross-provider effort maps onto the same on/off switch. Verified live
    # 2026-08-20 on both registered models: `enable_thinking: false` returns 200
    # with empty reasoning, not the 400 the undocumented switch risked.
    qwen_kwargs: dict[str, Any] = {}
    extra_body = qwen_extra_body(
        thinking=ctx.thinking,
        reasoning_effort=ctx.resolved_effort,
        effort_levels=(ctx.effort_levels if ctx.effort_levels is not None else _QWEN_EFFORT_LEVELS),
    )
    if extra_body:
        qwen_kwargs["extra_body"] = extra_body
    # stream_usage sends `stream_options.include_usage`, without which the
    # stream carries no final usage frame — and DashScope reports its implicit
    # context-cache hits in that frame's `prompt_tokens_details.cached_tokens`,
    # which langchain-openai maps onto usage_metadata's `cache_read` and the
    # cost ledger prices at the plugin's cache_read rate. Verified live
    # 2026-08-20 on both registered models: the streamed terminal frame does
    # carry the details object (cold call cached_tokens 0; warm repeat of a
    # ~2.7k-token prefix 2048 on max, 1664 on 27b), so the ledger reads a real
    # number rather than silently billing every turn as a full cache miss.
    return ReasoningContentChatModel(
        model=ctx.model,  # type: ignore[call-arg]
        api_key=api_key,  # type: ignore[arg-type]
        base_url=settings.lm.dashscope_base_url,
        stream_usage=True,
        disable_streaming=ctx.disable_streaming,
        timeout=ctx.timeout,
        **qwen_kwargs,
    )


register(
    ProviderBinding(
        prefix="qwen3.8-",
        provider_key="qwen",
        display_name="Alibaba",
        key_env="DASHSCOPE_API_KEY",
        build=build,
        effort_levels=_QWEN_EFFORT_LEVELS,
        vision=True,
        stop_spec=None,
    ),
    models={
        "qwen3.8-max": ModelSpec(
            provider="qwen",
            spawnable=True,
            # Alibaba publishes 991,808 max input and 983,616 "max input (thinking
            # mode)". This roster runs with thinking on, so the lower ceiling is the
            # one an agent actually has.
            context_window=983_616,
            max_output_tokens=131_072,
            # Alibaba publishes NO training-data cutoff for any Qwen model (checked
            # 2026-08-20 across the Model Studio model pages, the pricing page and
            # the Qwen team blog) — but `_validate_registry` requires one for a
            # spawnable model, and the value only feeds the system prompt's temporal
            # boundary. This is a deliberate conservative estimate anchored on the
            # model page's own publication (2026-08-03): erring EARLY is the safe
            # direction, since an over-late cutoff makes the agent trust stale
            # knowledge as current. Replace it the day Alibaba publishes one.
            knowledge_cutoff="2026-01",
            model_identity="You are running on Qwen3.8-Max (Alibaba Qwen).",
            # No graded effort field on the compatible-mode endpoint — the knob's
            # only wire effect is the `enable_thinking` on/off switch, same binary
            # as mimo.
            effort_levels=("none", "high"),
            tuning=ModelTuning(
                # The "high" rung IS the provider default: thinking is on for this
                # model unless `enable_thinking=false` is sent.
                reasoning_effort="high",
            ),
            media_types=frozenset({"image"}),
        ),
        "qwen3.8-27b": ModelSpec(
            provider="qwen",
            spawnable=True,
            # Same ceilings as qwen3.8-max (thinking-mode input is the binding one —
            # see the max entry); only price and weight class differ, at roughly a
            # quarter of max's input rate.
            context_window=983_616,
            max_output_tokens=131_072,
            # Same unpublished-cutoff situation as every Qwen — see qwen3.8-max.
            # Anchored on this model's own publication (2026-08-17), erring early.
            knowledge_cutoff="2026-01",
            model_identity="You are running on Qwen3.8-27B (Alibaba Qwen).",
            effort_levels=("none", "high"),
            tuning=ModelTuning(
                # Thinking on by default here too — verified live 2026-08-20 that
                # `enable_thinking: false` is honored rather than rejected.
                reasoning_effort="high",
            ),
            media_types=frozenset({"image"}),
        ),
        "qwen3.8-flash": ModelSpec(
            provider="qwen",
            spawnable=True,
            # DashScope model-info API (GET /api/v1/models, checked 2026-08-27):
            # context 1M, max input 991,808, reasoning-mode max input 983,616.
            # This roster runs with thinking on, so the lower ceiling is the one an
            # agent actually has (same convention as qwen3.8-max).
            context_window=983_616,
            max_output_tokens=131_072,
            # Same unpublished-cutoff situation as every Qwen — see qwen3.8-max.
            # Anchored on this model's own publication (2026-08-25), erring early.
            knowledge_cutoff="2026-01",
            model_identity="You are running on Qwen3.8-Flash (Alibaba Qwen).",
            # Same binary enable_thinking knob as every registered qwen model —
            # verified live 2026-08-27 that thinking is ON by default and
            # enable_thinking=false is honored.
            effort_levels=("none", "high"),
            tuning=ModelTuning(
                # The "high" rung IS the provider default: thinking is on unless
                # enable_thinking=false is sent (same as qwen3.8-max).
                reasoning_effort="high",
            ),
            # Official request modality is Image/Text/Video (DashScope model-info
            # API), but the compatible-mode binding renders image blocks only —
            # same conservatism as qwen3.8-max's entry.
            media_types=frozenset({"image"}),
        ),
    },
    pricing={
        "qwen3.8-max": PriceRates(
            cache_miss=1.65,
            cache_hit=0.206,
            output=4.951,
            source_url="https://www.alibabacloud.com/help/en/model-studio/qwen3-8-max",
            source_checked_at="2026-08-20",
            vendor="alibaba",
        ),
        "qwen3.8-27b": PriceRates(
            cache_miss=0.424,
            cache_hit=0.085,
            output=1.696,
            source_url="https://www.alibabacloud.com/help/en/model-studio/qwen3-8-27b",
            source_checked_at="2026-08-20",
            vendor="alibaba",
        ),
        "qwen3.8-flash": PriceRates(
            cache_miss=0.113,
            cache_hit=0.014,
            output=0.382,
            source_url="https://www.alibabacloud.com/help/en/model-studio/qwen3-8-flash",
            source_checked_at="2026-09-02",
            vendor="alibaba",
        ),
    },
)
