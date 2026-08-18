"""Cross-provider normalization of the LLM terminal (finish/stop) reason.

LangChain (core 1.4) standardizes tool_calls, usage_metadata, content_blocks,
and exposes response_metadata["model_provider"], but does NOT normalize the
finish/stop reason — each provider keeps its own key + vocabulary:

  anthropic     -> response_metadata["stop_reason"]   (end_turn / tool_use / refusal / max_tokens / ...)
  openai        -> response_metadata["finish_reason"] (stop / tool_calls / function_call / length / content_filter / ...)
  google_genai  -> response_metadata["finish_reason"] (STOP / MAX_TOKENS / SAFETY / RECITATION / ...)

`classify_stop` maps any of them to a provider-agnostic StopCategory, dispatched
on model_provider. Unknown provider raises (fail-fast — add a branch when a new
provider is introduced in shared/lm/factory.py:build_chat_model).
"""

import enum
from typing import Literal, NamedTuple

from langchain_core.messages import AIMessage

from shared.message_kwargs import message_response_metadata


class StopCategory(enum.Enum):
    NORMAL = "normal"  # model finished its turn (talk / tool / refusal) -> existing paths
    TRUNCATED = "truncated"  # server output budget exhausted -> bump max_tokens, retry
    UNEXPECTED = (
        "unexpected"  # safety / content_filter / recitation etc. -> fail-fast, user sees it
    )
    CORRUPTED = "corrupted"  # terminal reason missing -> protocol drift / lost final frame


# model_provider values build_chat_model emits (response_metadata["model_provider"]).
# Adding a provider in shared/lm/factory.py means adding it here too — keeping this a
# Literal makes pyright flag a _BY_PROVIDER key that drifts from this set.
ProviderKey = Literal["anthropic", "openai", "google_genai", "moonshot", "xai"]


class _ProviderSpec(NamedTuple):
    """How to read + classify one provider's terminal reason.

    `key` is the response_metadata field that carries it; a value in `normal`
    is a clean turn end, one in `truncated` is an output-budget cutoff; anything
    else present is UNEXPECTED; the key missing is CORRUPTED.
    """

    key: str
    normal: frozenset[str]
    truncated: frozenset[str]


_BY_PROVIDER: dict[ProviderKey, _ProviderSpec] = {
    "anthropic": _ProviderSpec(
        "stop_reason", frozenset({"end_turn", "tool_use", "refusal"}), frozenset({"max_tokens"})
    ),
    "openai": _ProviderSpec(
        "finish_reason", frozenset({"stop", "tool_calls", "function_call"}), frozenset({"length"})
    ),
    "google_genai": _ProviderSpec("finish_reason", frozenset({"STOP"}), frozenset({"MAX_TOKENS"})),
    # OpenAI-compatible providers — ChatMoonshot / ChatXAI both inherit from
    # BaseChatOpenAI and use the same finish_reason vocabulary.
    "moonshot": _ProviderSpec(
        "finish_reason", frozenset({"stop", "tool_calls", "function_call"}), frozenset({"length"})
    ),
    "xai": _ProviderSpec(
        "finish_reason", frozenset({"stop", "tool_calls", "function_call"}), frozenset({"length"})
    ),
}


def classify_stop(final_msg: AIMessage) -> tuple[StopCategory, str | None]:
    """Return (category, raw_reason) for the message's terminal reason.

    Raises:
        ValueError: model_provider is missing or not one build_chat_model emits —
            a new provider branch must add its vocabulary here.
    """
    metadata = message_response_metadata(final_msg) or {}
    provider = metadata.get("model_provider")
    # provider is a runtime string; a value outside the ProviderKey literal simply
    # misses the dict (spec stays None) and is rejected just below.
    spec = _BY_PROVIDER.get(provider) if isinstance(provider, str) else None  # pyright: ignore[reportArgumentType]
    if spec is None:
        raise ValueError(
            f"unknown model_provider {provider!r} (metadata keys={list(metadata.keys())!r}); "
            f"add its terminal-reason vocabulary to shared/lm/stop.py"
        )
    raw = metadata.get(spec.key)
    if raw is None:
        # OpenAI Responses API (use_responses_api=True) returns `status`
        # (e.g. "completed") instead of `finish_reason`.  Map it to the
        # same StopCategory so the validation path treats it as a clean
        # turn end, not a corrupted protocol frame.
        if provider == "openai" and "status" in metadata:
            raw = metadata["status"]
            if raw == "completed":
                return StopCategory.NORMAL, raw
            if raw == "incomplete":
                return StopCategory.TRUNCATED, raw
        return StopCategory.CORRUPTED, None
    if raw in spec.normal:
        return StopCategory.NORMAL, raw
    if raw in spec.truncated:
        return StopCategory.TRUNCATED, raw
    return StopCategory.UNEXPECTED, raw
