"""Per-provider chat-model builders — one `_build_*_model` per model prefix.

Companion module of `shared/lm/factory.py` (split for the file-size ceiling,
same pattern as `_effort.py` / `registry.py`): factory keeps the catalog
surface + the prefix dispatch, this module owns provider construction. Each
builder documents its own key / thinking / reasoning-effort wiring; the
cross-provider resolution shared by every branch (AVA_LLM_OVERRIDE, the
streaming default, the reasoning-effort knob) happens in `build_chat_model`
before dispatch. See the factory module docstring for the provider matrix.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, NotRequired, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from loguru import logger

from shared.config import settings
from shared.lm._effort import (
    _PROVIDER_EFFORT_LEVELS,
    _clamp_effort,
    claude_extended_thinking_kwarg,
    deepseek_wire_effort,
    mimo_extra_body,
    qwen_extra_body,
)
from shared.lm.registry import MODELS, ModelSpec, resolve_setting
from shared.turn_identity import effective_agent_id


class ThinkingConfig(TypedDict):
    """The Anthropic extended-thinking config passed to `build_chat_model`.

    `{"type": "disabled"}` turns thinking off (short-text paths); `{"type":
    "enabled", "budget_tokens": N}` turns it on with a token budget;
    `{"type": "adaptive"}` (adaptive-thinking claude models only) lets the
    model decide whether to think, with `display` choosing summarized text
    vs signature-only. Only the claude / deepseek branches pass the dict
    through on the wire; every other branch reads `type` and mirrors
    disabled onto its own switch (gemini thinking_level, gpt
    reasoning.effort, mimo / glm body thinking) — kimi / grok cannot
    disable reasoning and log a warning instead.
    """

    type: Literal["enabled", "disabled", "adaptive"]
    budget_tokens: NotRequired[int]
    display: NotRequired[Literal["summarized", "omitted"]]


# DeepSeek's anthropic-compatible endpoint. Also the single source of truth
# for model name → endpoint resolution; not written twice — to change the
# endpoint, change here.
_DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"


# xAI cache-affinity routing: xAI recommends a stable `x-grok-conv-id` header
# so repeated requests land on the same backend cache shard (~5pp better
# prompt-cache hit rate). Ava scopes the id per agent — an agent's turns are
# exactly the continuity worth pinning. Agent processes carry AVA_AGENT_ID in
# env (set by the launcher), so a deterministic uuid5 survives restarts;
# outside an agent process (cli / gateway / tests) fall back to one random
# UUID per process.
_GROK_CONV_ID_NAMESPACE = uuid.UUID("fe5c4bbc-c3a6-514c-92a0-5d99fa6d242f")
_PROCESS_CONV_ID = str(uuid.uuid4())


def _grok_conv_id() -> str:
    # Turn contextvar > AVA_AGENT_ID env: in the hosted runner one process
    # serves many agents, and cache affinity must follow the turn's agent.
    agent_id = effective_agent_id()
    if agent_id is not None:
        return str(uuid.uuid5(_GROK_CONV_ID_NAMESPACE, f"ava-agent-{agent_id}"))
    return _PROCESS_CONV_ID


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


def _build_deepseek_model(
    model: str,
    spec: ModelSpec | None,
    thinking: ThinkingConfig | None,
    reasoning_effort: str | None,
    resolved_effort: str,
    extra_kwargs: dict[str, Any],
    *,
    timeout: float | None = None,
) -> BaseChatModel:
    """deepseek-* branch: ChatAnthropic on the anthropic-compatible
    endpoint, pinned max_tokens. Reasoning effort rides
    `output_config.effort` via extra_body; `none` maps onto thinking
    disabled (the endpoint's effort vocabulary has no off level).
    """
    from shared.lm._anthropic_compat import ThinkingTokensChatAnthropic

    # Use DeepSeek's anthropic-compatible endpoint via ChatAnthropic client.
    # Not langchain-deepseek (1.0.1 reasoning_content roundtrip bug).
    # api_key comes from DEEPSEEK_API_KEY (settings.lm.deepseek_api_key);
    # if missing, RuntimeError fail-fast immediately — don't silently use
    # wrong key and only find out from a server 401.
    if settings.lm.deepseek_api_key is None:
        raise RuntimeError(
            "DEEPSEEK_API_KEY not set — deepseek-* model needs this key; "
            "configure in ~/.ava/.env or export before starting"
        )
    api_key = settings.lm.deepseek_api_key

    # Per-model output cap (ModelSpec.max_output_tokens). An unregistered
    # deepseek model fails fast here rather than borrowing a wrong cap —
    # the same fail-fast posture as the unknown-prefix raise at the end.
    if spec is None or spec.max_output_tokens is None:
        raise ValueError(
            f"Unknown deepseek model {model!r} — register it (with "
            f"max_output_tokens) in `shared/lm/registry.py:MODELS`"
        )
    max_tokens = spec.max_output_tokens

    # Reasoning effort: DeepSeek's anthropic-compat endpoint accepts
    # `output_config.effort` to control reasoning depth (the official
    # benchmark 80.6% Verified runs the max mode). Note: the standard
    # anthropic `budget_tokens` is *ignored* on this endpoint (DeepSeek
    # docs explicitly say so); must go through output_config.
    # Passed via model_kwargs.extra_body to the underlying Anthropic SDK,
    # which merges extra_body into the POST body without touching schema.
    #
    # thinking={"type":"disabled"} is mutually exclusive with reasoning_effort
    # (server 400: "thinking options type cannot be disabled when
    # reasoning_effort is set"). Caller explicitly disabling thinking →
    # also skip effort injection; don't let global env sneakily push
    # reasoning back (the labeler short-text path is exactly this case).
    #
    # The effort goes through the same per-model clamp as claude
    # (`ModelSpec.effort_levels`), and "none" — the only cross-provider
    # value that means off rather than a level — lands on the endpoint's
    # own off-switch, `thinking={"type":"disabled"}` (the mimo branch makes
    # the same mapping). DeepSeek's vocabulary has no `none` variant, so
    # sending it unclamped 400s the request: that is what took every
    # `ava.web.fetch` down, since AVA_WEB_FETCH_REASONING ships as "none".
    deepseek_model_kwargs: dict[str, Any] = {}
    thinking_disabled = thinking is not None and thinking.get("type") == "disabled"
    effort = reasoning_effort or resolved_effort
    if effort and not thinking_disabled and spec.effort_levels is not None:
        wire_effort = deepseek_wire_effort(effort, spec.effort_levels, target=model)
        if wire_effort is not None:
            deepseek_model_kwargs["extra_body"] = {"output_config": {"effort": wire_effort}}
        elif thinking is None:
            # A caller-passed `thinking` states its own intent and wins.
            extra_kwargs["thinking"] = {"type": "disabled"}

    # type ignore same as claude branch: langchain-anthropic stub treats
    # model / api_key / base_url / max_tokens as pydantic alias; signature
    # only exposes canonical field names model_name / anthropic_api_key /
    # anthropic_api_url / max_tokens_to_sample; runtime goes through
    # alias. Each parameter line of a multi-line call needs its own
    # ignore — pyright does not propagate from the opening paren line to
    # subsequent keyword argument lines.
    return ThinkingTokensChatAnthropic(
        model=model,  # type: ignore[call-arg]
        api_key=api_key,
        base_url=_DEEPSEEK_ANTHROPIC_BASE_URL,
        max_tokens=max_tokens,  # type: ignore[call-arg]
        model_kwargs=deepseek_model_kwargs,
        timeout=timeout,
        **extra_kwargs,
    )


def _build_gemini_model(
    model: str,
    *,
    thinking: ThinkingConfig | None,
    resolved_effort: str,
    disable_streaming: bool,
    timeout: float | None = None,
) -> BaseChatModel:
    """gemini-* branch: ChatGoogleGenerativeAI. include_thoughts surfaces
    the thinking summary as canonical thinking blocks; thinking depth
    rides thinking_level (cannot fully turn off — disabled maps to
    "minimal" + include_thoughts=False)."""
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
    # the lowest level "minimal" + include_thoughts=False — dropping
    # include_thoughts alone would only hide the thought blocks while the
    # model keeps thinking (and billing) at its default level. Otherwise
    # the cross-provider effort maps onto the thinking_level vocabulary;
    # unset effort leaves thinking_level None → the model default
    # (3.6/3.5 Flash medium, 3.1 Pro high).
    thinking_disabled = thinking is not None and thinking.get("type") == "disabled"
    thinking_level: str | None = None
    if thinking_disabled:
        # thinking_level is a Gemini 3.x vocabulary; older models
        # (gemini-2.5-*) reject it with a 400 on every call (issue #190), so
        # "disabled" is only expressible where the model declares the
        # vocabulary. Elsewhere it is a no-op — no thinking parameters on the
        # wire, exactly what the issue measured as working — plus a warning
        # so the operator knows the request was not honored.
        spec = MODELS.get(model)
        if spec is not None and spec.effort_levels is not None:
            thinking_level = "minimal"
        else:
            logger.warning(
                f"{model} does not support the thinking_level vocabulary; "
                f"thinking={{'type': 'disabled'}} ignored (issue #190)"
            )
    elif resolved_effort:
        thinking_level = _clamp_effort(
            resolved_effort,
            _PROVIDER_EFFORT_LEVELS["gemini"],
            target="gemini",
        )
    # include_thoughts only on the vocabulary-supporting path: passing it to a
    # model that rejects thinking parameters would 400 exactly like
    # thinking_level (issue #190). Unsupported models keep the SDK default;
    # the non-disabled path keeps its historical include_thoughts=True.
    if thinking_disabled and thinking_level is None:
        include_thoughts: bool | None = None
    else:
        include_thoughts = not thinking_disabled
    return ChatGoogleGenerativeAI(
        model=model,  # type: ignore[call-arg]
        google_api_key=settings.lm.gemini_api_key.get_secret_value(),  # type: ignore[arg-type]
        include_thoughts=include_thoughts,
        thinking_level=thinking_level,  # type: ignore[arg-type]
        disable_streaming=disable_streaming,
        timeout=timeout,
    )


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
    *,
    thinking: ThinkingConfig | None,
    resolved_effort: str,
    disable_streaming: bool,
    timeout: float | None = None,
) -> BaseChatModel:
    """glm-* branch: ReasoningContentChatModel (OpenAI-compatible).
    Thinking off rides top-level `thinking` via extra_body; otherwise
    reasoning effort rides the `reasoning_effort` constructor field."""
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
    # field (GLM's enum is high/max, default max — `high` is the only
    # cheaper tier).
    glm_kwargs: dict[str, Any] = {}
    if thinking is not None and thinking.get("type") == "disabled":
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


def _build_grok_model(
    model: str,
    *,
    thinking: ThinkingConfig | None,
    resolved_effort: str,
    disable_streaming: bool,
    timeout: float | None = None,
) -> BaseChatModel:
    """grok-* branch: ChatXAI (OpenAI-compatible). Grok 4.5's reasoning
    cannot be turned off — a caller disabling it gets a warning. A stable
    `x-grok-conv-id` header pins the agent's requests to the same backend
    cache shard."""
    from langchain_xai import ChatXAI

    # xAI Grok API is OpenAI-compatible (https://api.x.ai/v1),
    # standard `Authorization: Bearer` auth. Grok streams thinking in the
    # delta's `reasoning_content` field. ChatXAI captures reasoning in
    # `additional_kwargs["reasoning_content"]`; the streaming fan-out
    # (`RedisStreamHandler`) and timeline (`shared/timeline.py`) both read
    # that key so reasoning renders through the same path.
    if settings.lm.xai_api_key is None:
        raise RuntimeError(
            "XAI_API_KEY not set — grok-* model needs this key; "
            "configure in ~/.ava/.env or export before starting"
        )
    # Grok 4.5's reasoning cannot be turned off — warn instead of silently
    # dropping the caller's intent.
    if thinking is not None and thinking.get("type") == "disabled":
        logger.warning(f"{model} cannot disable reasoning; thinking={{'type': 'disabled'}} ignored")
    # Reasoning effort rides the top-level `reasoning_effort` body field,
    # delivered via extra_body — the langchain-xai documented shape.
    # (Grok's enum is low/medium/high, default high.)
    grok_kwargs: dict[str, Any] = {}
    if resolved_effort:
        grok_kwargs["extra_body"] = {
            "reasoning_effort": _clamp_effort(
                resolved_effort,
                _PROVIDER_EFFORT_LEVELS["grok"],
                target="grok",
            )
        }
    # Cache-affinity routing header — see _grok_conv_id above.
    grok_kwargs["default_headers"] = {"x-grok-conv-id": _grok_conv_id()}
    return ChatXAI(
        model=model,  # type: ignore[call-arg]
        xai_api_key=settings.lm.xai_api_key.get_secret_value(),  # type: ignore[arg-type]
        stream_usage=True,
        disable_streaming=disable_streaming,
        timeout=timeout,
        **grok_kwargs,
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
