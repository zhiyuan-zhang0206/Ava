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
    claude_extended_thinking_kwarg,
    mimo_extra_body,
    qwen_extra_body,
)
from shared.lm.registry import MODELS, ModelSpec, resolve_setting


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


def _build_claude_model(
    model: str,
    spec: ModelSpec | None,
    thinking: ThinkingConfig | None,
    resolved_effort: str,
    extra_kwargs: dict[str, Any],
    *,
    timeout: float | None = None,
) -> BaseChatModel:
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
    if settings.lm.anthropic_api_key is None:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set — claude-* model needs this key; "
            "configure in ~/.ava/.env or export before starting"
        )

    # Per-model output cap (ModelSpec.max_output_tokens). Same fail-fast
    # posture as the deepseek branch — an unregistered claude model raises
    # rather than falling back to langchain's stale profile table (4096
    # for unknown ids).
    if spec is None or spec.max_output_tokens is None:
        raise ValueError(
            f"Unknown claude model {model!r} — register it (with "
            f"max_output_tokens) in `shared/lm/registry.py:MODELS`"
        )

    thinking_disabled = thinking is not None and thinking.get("type") == "disabled"

    # Reasoning effort rides ChatAnthropic's `effort` field
    # (output_config.effort on the wire), gated per model — extended-
    # thinking-only models (haiku-4-5) have no effort support (server 400)
    # and map the knob onto their thinking on/off binary below instead.
    # Caller-disabled thinking skips effort injection, mirroring deepseek:
    # an explicit cheap/fast path shouldn't have the global env push
    # reasoning back in.
    claude_kwargs: dict[str, Any] = {}
    effort = resolved_effort
    if effort and not thinking_disabled and not spec.extended_thinking_only:
        if spec.effort_levels is not None:
            claude_kwargs["effort"] = _clamp_effort(effort, spec.effort_levels, target=model)
        else:
            logger.warning(
                f"{model} does not support reasoning effort; "
                f"AVA_REASONING_EFFORT={effort!r} ignored"
            )

    # Extended-thinking-only models (haiku-4-5) map budget_tokens / effort
    # onto their thinking on/off binary — see
    # shared/lm/_effort.py:claude_extended_thinking_kwarg.
    extended_thinking = claude_extended_thinking_kwarg(
        model,
        thinking=thinking,
        budget_tokens=resolve_setting("claude_thinking_budget_tokens", model=model),
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
            dict(thinking) if thinking is not None else {"type": "adaptive"}
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
        model=model,  # type: ignore[call-arg]
        api_key=settings.lm.anthropic_api_key,
        max_tokens=spec.max_output_tokens,  # type: ignore[call-arg]
        model_kwargs={"cache_control": {"type": "ephemeral"}},
        timeout=timeout,
        **claude_kwargs,
        **extra_kwargs,
    )


def _build_gemini_model(
    model: str,
    *,
    thinking: ThinkingConfig | None,
    resolved_effort: str,
    disable_streaming: bool,
    timeout: float | None = None,
    media_resolution: str | None = None,
    media_thinking_level: str | None = None,
    base_url: str | None = None,
) -> BaseChatModel:
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
    if settings.lm.gemini_api_key is None:
        raise RuntimeError(
            "GEMINI_API_KEY not set — gemini-* model needs this key; "
            "configure in ~/.ava/.env or export before starting"
        )
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
    thinking_disabled = thinking is not None and thinking.get("type") == "disabled"
    thinking_level: str | None = None
    if media_thinking_level is not None:
        # Media path — the caller owns the Gemini vocabulary mapping
        # (ava/understand.py), including the `max` → configured-knob
        # special case; the resolved-effort path is skipped entirely so a
        # global AVA_REASONING_EFFORT cannot silently override the media
        # knob (historic behavior).
        thinking_level = media_thinking_level
    elif thinking_disabled:
        # thinking_level is a Gemini 3.x vocabulary; older models
        # (gemini-2.5-*) reject it with a 400 on every call (issue #190), so
        # "disabled" is only expressible where the model declares the
        # vocabulary. Elsewhere it is a no-op — no thinking parameters on the
        # wire, exactly what the issue measured as working — plus a warning
        # so the operator knows the request was not honored.
        spec = MODELS.get(model)
        if spec is not None and spec.effort_levels is not None:
            thinking_level = spec.effort_levels[0] if spec.effort_levels else "minimal"
        else:
            logger.warning(
                f"{model} does not support the thinking_level vocabulary; "
                f"thinking={{'type': 'disabled'}} ignored (issue #190)"
            )
    elif resolved_effort:
        # A declared model vocabulary is authoritative: 3.8 Flash does not
        # accept `minimal`, even though the Gemini-wide fallback includes it.
        spec = MODELS.get(model)
        levels = (
            spec.effort_levels
            if spec is not None and spec.effort_levels
            else _PROVIDER_EFFORT_LEVELS["gemini"]
        )
        thinking_level = _clamp_effort(
            resolved_effort,
            levels,
            target="gemini",
        )
    # include_thoughts only on the vocabulary-supporting path: passing it to a
    # model that rejects thinking parameters would 400 exactly like
    # thinking_level (issue #190). Unsupported models keep the SDK default;
    # the non-disabled path keeps its historical include_thoughts=True.
    if media_thinking_level is not None:
        # Media path historic behavior: the field was never set on the
        # ChatGoogleGenerativeAI constructor, so leave it at the SDK default
        # (None) — do not surface thought blocks.
        include_thoughts: bool | None = None
    elif thinking_disabled and thinking_level is None:
        include_thoughts = None
    else:
        include_thoughts = not thinking_disabled
    # Media-resolution mapping (ava.understand's media path). The enum import
    # is lazy like the model class itself so the google-genai SDK binding
    # stays inside this provider branch.
    media_resolution_value: Any | None = None
    if media_resolution is not None:
        from google.genai.types import MediaResolution

        resolutions = {
            "low": MediaResolution.MEDIA_RESOLUTION_LOW,
            "medium": MediaResolution.MEDIA_RESOLUTION_MEDIUM,
            "high": MediaResolution.MEDIA_RESOLUTION_HIGH,
        }
        try:
            media_resolution_value = resolutions[media_resolution]
        except KeyError:
            raise ValueError(
                f"media_resolution must be one of {sorted(resolutions)}, got {media_resolution!r}"
            ) from None
    kwargs: dict[str, Any] = {
        "model": model,  # type: ignore[call-arg]
        "google_api_key": settings.lm.gemini_api_key.get_secret_value(),  # type: ignore[arg-type]
        "include_thoughts": include_thoughts,
        "thinking_level": thinking_level,  # type: ignore[arg-type]
        "disable_streaming": disable_streaming,
        "timeout": timeout,
    }
    if media_resolution_value is not None:
        kwargs["media_resolution"] = media_resolution_value
    if base_url is not None:
        kwargs["base_url"] = base_url
    return ChatGoogleGenerativeAI(**kwargs)


def _build_gpt_model(
    model: str,
    *,
    thinking: ThinkingConfig | None,
    resolved_effort: str,
    disable_streaming: bool,
    timeout: float | None = None,
) -> BaseChatModel:
    """gpt-* branch: ChatOpenAI on the Responses API. `reasoning.{effort,
    summary}` returns the reasoning summary as a canonical block; a
    caller disabling thinking drops to effort "none". (The Chat
    Completions path returns zero reasoning.)"""
    from langchain_openai import ChatOpenAI

    # Direct construction, same rationale as the gemini- branch.
    if settings.lm.openai_api_key is None:
        raise RuntimeError(
            "OPENAI_API_KEY not set — gpt-* model needs this key; "
            "configure in ~/.ava/.env or export before starting"
        )
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
    thinking_disabled = thinking is not None and thinking.get("type") == "disabled"
    gpt_effort = resolved_effort or "medium"
    gpt_reasoning: dict[str, Any] = (
        {"effort": "none"} if thinking_disabled else {"effort": gpt_effort, "summary": "auto"}
    )
    return ChatOpenAI(
        model=model,  # type: ignore[call-arg]
        api_key=settings.lm.openai_api_key.get_secret_value(),  # type: ignore[arg-type]
        use_responses_api=True,
        reasoning=gpt_reasoning,
        disable_streaming=disable_streaming,
        timeout=timeout,
    )


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
