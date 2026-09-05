"""Core chat-model builders — one `_build_*_model` per remaining core prefix.

Companion module of `shared/lm/factory.py` (split for the file-size ceiling,
same pattern as `_effort.py` / `registry.py`): factory keeps the catalog
surface + the prefix dispatch, this module owns provider construction. Each
builder documents its own key / thinking / reasoning-effort wiring; the
cross-provider resolution shared by every branch (AVA_LLM_OVERRIDE, the
streaming default, the reasoning-effort knob) happens in `build_chat_model`
before dispatch. See the factory module docstring for the provider matrix.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from loguru import logger

from shared.config import settings
from shared.lm._effort import (
    _PROVIDER_EFFORT_LEVELS,
    _clamp_effort,
    mimo_extra_body,
    qwen_extra_body,
)
from shared.lm.registry import ModelSpec


class ThinkingConfig(TypedDict):
    """The Anthropic extended-thinking config passed to `build_chat_model`.

    `{"type": "disabled"}` turns thinking off (short-text paths); `{"type":
    "enabled", "budget_tokens": N}` turns it on with a token budget;
    `{"type": "adaptive"}` (adaptive-thinking claude models only) lets the
    model decide whether to think, with `display` choosing summarized text
    vs signature-only. Only the claude / deepseek branches pass the dict
    through on the wire; every other branch reads `type` and mirrors
    disabled onto its own switch (gemini thinking_level, gpt
    reasoning.effort, mimo / glm body thinking) — kimi cannot
    disable reasoning and log a warning instead.
    """

    type: Literal["enabled", "disabled", "adaptive"]
    budget_tokens: NotRequired[int]
    display: NotRequired[Literal["summarized", "omitted"]]


def _build_mimo_model(
    model: str,
    *,
    thinking: ThinkingConfig | None,
    resolved_effort: str,
    disable_streaming: bool,
    timeout: float | None = None,
) -> BaseChatModel:
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
    if settings.lm.xiaomi_api_key is None:
        raise RuntimeError(
            "MIMO_API_KEY not set — mimo-* model needs this key; "
            "configure in ~/.ava/.env or export before starting"
        )
    api_key = settings.lm.xiaomi_api_key.get_secret_value()
    # MiMo natively supports switching deep thinking off — top-level
    # `thinking` in the POST body, delivered via extra_body (declared on
    # BaseChatOpenAI; model_kwargs would collide). No graded
    # reasoning_effort field on MiMo — see
    # shared/lm/_effort.py:mimo_extra_body for the on/off mapping.
    mimo_kwargs: dict[str, Any] = {}
    extra_body = mimo_extra_body(thinking=thinking, reasoning_effort=resolved_effort)
    if extra_body:
        mimo_kwargs["extra_body"] = extra_body
    return ReasoningContentChatModel(
        model=model,  # type: ignore[call-arg]
        api_key=api_key,  # type: ignore[arg-type]
        base_url="https://api.xiaomimimo.com/v1",
        default_headers={"api-key": api_key},
        stream_usage=True,
        disable_streaming=disable_streaming,
        timeout=timeout,
        **mimo_kwargs,
    )


def _build_kimi_model(
    model: str,
    *,
    thinking: ThinkingConfig | None,
    resolved_effort: str,
    disable_streaming: bool,
    timeout: float | None = None,
) -> BaseChatModel:
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
    if settings.lm.moonshot_api_key is None:
        raise RuntimeError(
            "MOONSHOT_API_KEY not set — kimi-* model needs this key; "
            "configure in ~/.ava/.env or export before starting"
        )
    # K3's thinking is always on and cannot be turned off, and the
    # K2.x-era `thinking` request parameter is not accepted by K3 — warn
    # instead of silently dropping the caller's intent. (Do NOT wire
    # ChatMoonshot's `thinking` field: it emits that K2.x parameter.)
    if thinking is not None and thinking.get("type") == "disabled":
        logger.warning(f"{model} cannot disable thinking; thinking={{'type': 'disabled'}} ignored")
    # Reasoning effort rides the top-level `reasoning_effort` body field,
    # delivered via extra_body (Kimi's enum is low/high/max, default max —
    # the only way to run K3 below its most expensive tier).
    kimi_kwargs: dict[str, Any] = {}
    if resolved_effort:
        kimi_kwargs["extra_body"] = {
            "reasoning_effort": _clamp_effort(
                resolved_effort,
                _PROVIDER_EFFORT_LEVELS["kimi"],
                target="kimi",
            )
        }
    return ChatMoonshot(
        model=model,  # type: ignore[call-arg]
        api_key=settings.lm.moonshot_api_key.get_secret_value(),  # type: ignore[arg-type]
        stream_usage=True,
        disable_streaming=disable_streaming,
        timeout=timeout,
        **kimi_kwargs,
    )


def _build_glm_model(
    model: str,
    spec: ModelSpec | None,
    *,
    thinking: ThinkingConfig | None,
    resolved_effort: str,
    disable_streaming: bool,
    timeout: float | None = None,
) -> BaseChatModel:
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
    if settings.lm.zhipu_api_key is None:
        raise RuntimeError(
            "GLM_API_KEY not set — glm-* model needs this key; "
            "configure in ~/.ava/.env or export before starting"
        )
    # GLM natively supports turning thinking off — top-level `thinking` in
    # the POST body via extra_body. Disabled thinking skips effort
    # injection (deepseek-style: the caller asked for the cheap path).
    # Otherwise reasoning effort rides the OpenAI-standard
    # `reasoning_effort` payload field, a declared ChatOpenAI constructor
    # field (GLM's enum is low/high/max, default max — `low` and `high` are
    # the cheaper tiers; checked 2026-08-23 for GLM-5.3).
    glm_kwargs: dict[str, Any] = {}
    if thinking is not None and thinking.get("type") == "disabled":
        if spec is not None and spec.thinking_always_on:
            # glm-5.3 / glm-5.3-flash always think — the endpoint rejects
            # thinking.type=disabled (400, error code 1210, live-checked
            # 2026-08-27). Warn like the kimi branch instead of sending a
            # body that fails the call.
            logger.warning(
                f"{model} cannot disable thinking; thinking={{'type': 'disabled'}} ignored"
            )
        else:
            glm_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    elif resolved_effort:
        glm_kwargs["reasoning_effort"] = _clamp_effort(
            resolved_effort,
            _PROVIDER_EFFORT_LEVELS["glm"],
            target="glm",
        )
    return ReasoningContentChatModel(
        model=model,  # type: ignore[call-arg]
        api_key=settings.lm.zhipu_api_key.get_secret_value(),  # type: ignore[arg-type]
        base_url="https://open.bigmodel.cn/api/paas/v4",
        stream_usage=True,
        disable_streaming=disable_streaming,
        timeout=timeout,
        **glm_kwargs,
    )


def _build_qwen_model(
    model: str,
    *,
    thinking: ThinkingConfig | None,
    resolved_effort: str,
    disable_streaming: bool,
    timeout: float | None = None,
) -> BaseChatModel:
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
    # repointing it means re-checking pricing_catalog.json too.
    # Qwen streams its thinking in the delta's `reasoning_content` field,
    # which ReasoningContentChatModel recovers into canonical thinking blocks —
    # the same reason glm / mimo use it rather than bare ChatOpenAI.
    if settings.lm.dashscope_api_key is None:
        raise RuntimeError(
            "DASHSCOPE_API_KEY not set — qwen* model needs this key; "
            "configure in ~/.ava/.env or export before starting"
        )
    # The registered Qwen roster thinks by default and can be switched off with
    # the body-level `enable_thinking` boolean, delivered via extra_body
    # (declared on BaseChatOpenAI; model_kwargs would collide). DashScope's
    # graded knob is a token budget (`thinking_budget`), not a level enum, so
    # the cross-provider effort maps onto the same on/off switch — see
    # shared/lm/_effort.py:qwen_extra_body. Verified live 2026-08-20 on both
    # registered models: `enable_thinking: false` returns 200 with empty
    # reasoning, not the 400 the undocumented switch risked.
    qwen_kwargs: dict[str, Any] = {}
    extra_body = qwen_extra_body(thinking=thinking, reasoning_effort=resolved_effort)
    if extra_body:
        qwen_kwargs["extra_body"] = extra_body
    # stream_usage sends `stream_options.include_usage`, without which the
    # stream carries no final usage frame — and DashScope reports its implicit
    # context-cache hits in that frame's `prompt_tokens_details.cached_tokens`,
    # which langchain-openai maps onto usage_metadata's `cache_read` and the
    # cost ledger prices at the catalog's cache_read rate. Verified live
    # 2026-08-20 on both registered models: the streamed terminal frame does
    # carry the details object (cold call cached_tokens 0; warm repeat of a
    # ~2.7k-token prefix 2048 on max, 1664 on 27b), so the ledger reads a real
    # number rather than silently billing every turn as a full cache miss.
    return ReasoningContentChatModel(
        model=model,  # type: ignore[call-arg]
        api_key=settings.lm.dashscope_api_key.get_secret_value(),  # type: ignore[arg-type]
        base_url=settings.lm.dashscope_base_url,
        stream_usage=True,
        disable_streaming=disable_streaming,
        timeout=timeout,
        **qwen_kwargs,
    )
