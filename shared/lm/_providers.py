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

from shared.config import settings
from shared.lm._effort import mimo_extra_body


class ThinkingConfig(TypedDict):
    """The Anthropic extended-thinking config passed to `build_chat_model`.

    `{"type": "disabled"}` turns thinking off (short-text paths); `{"type":
    "enabled", "budget_tokens": N}` turns it on with a token budget;
    `{"type": "adaptive"}` (adaptive-thinking claude models only) lets the
    model decide whether to think, with `display` choosing summarized text
    vs signature-only. Only the claude / deepseek branches pass the dict
    through on the wire; every other branch reads `type` and mirrors
    disabled onto its own switch (gemini thinking_level, gpt
    reasoning.effort, mimo body thinking) — kimi cannot disable reasoning
    and logs a warning instead.
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
