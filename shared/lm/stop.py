"""Cross-provider normalization of the LLM terminal (finish/stop) reason.

LangChain (core 1.4) standardizes tool_calls, usage_metadata, content_blocks,
and exposes response_metadata["model_provider"], but does NOT normalize the
finish/stop reason — each provider keeps its own key + vocabulary:

  anthropic     -> response_metadata["stop_reason"]   (end_turn / tool_use / refusal / max_tokens / ...)
  openai        -> response_metadata["finish_reason"] (stop / tool_calls / function_call / length / content_filter / ...)
  google_genai  -> response_metadata["finish_reason"] (STOP / MAX_TOKENS / SAFETY / RECITATION / ...)

`classify_stop` maps any of them to a provider-agnostic StopCategory, dispatched
on model_provider. Unknown provider raises (fail-fast — a plugin provider
registers its vocabulary via register_stop_spec; see shared/lm/provider_api.py).
"""

import enum
from typing import NamedTuple

from langchain_core.messages import AIMessage

from shared.message_kwargs import message_response_metadata


class StopCategory(enum.Enum):
    NORMAL = "normal"  # model finished its turn (talk / tool / refusal) -> existing paths
    TRUNCATED = "truncated"  # server output budget exhausted -> bump max_tokens, retry
    UNEXPECTED = (
        "unexpected"  # safety / content_filter / recitation etc. -> fail-fast, user sees it
    )
    CORRUPTED = "corrupted"  # terminal reason missing -> protocol drift / lost final frame


# model_provider values the bound client classes emit
# (response_metadata["model_provider"]). A provider bound through a client class
# already in this table inherits its entry; a plugin binding a client class
# whose model_provider string is not here must register one via
# register_stop_spec (shared/lm/provider_api.py wires it through) or the first
# turn that ends raises ValueError.
class StopSpec(NamedTuple):
    """How to read + classify one provider's terminal reason.

    `provider_key` is the emitted ``model_provider`` value. `key` is the
    response_metadata field that carries the terminal reason; a value in
    `normal` is a clean turn end, one in `truncated` is an output-budget
    cutoff; anything else present is UNEXPECTED; the key missing is CORRUPTED.
    """

    provider_key: str
    key: str
    normal: frozenset[str]
    truncated: frozenset[str]


_BY_PROVIDER: dict[str, StopSpec] = {
    "anthropic": StopSpec(
        "anthropic",
        "stop_reason",
        frozenset({"end_turn", "tool_use", "refusal"}),
        frozenset({"max_tokens"}),
    ),
    "openai": StopSpec(
        "openai",
        "finish_reason",
        frozenset({"stop", "tool_calls", "function_call"}),
        frozenset({"length"}),
    ),
    "google_genai": StopSpec(
        "google_genai", "finish_reason", frozenset({"STOP"}), frozenset({"MAX_TOKENS"})
    ),
    # ChatMoonshot is OpenAI-compatible and uses this finish_reason vocabulary.
    "moonshot": StopSpec(
        "moonshot",
        "finish_reason",
        frozenset({"stop", "tool_calls", "function_call"}),
        frozenset({"length"}),
    ),
}


def register_stop_spec(spec: StopSpec, *, plugin: str = "<unknown>") -> None:
    """Register a plugin provider's terminal-reason vocabulary.

    Key is the ``model_provider`` string the plugin's client class emits. A
    key already present is an error — two different client classes never
    share a model_provider string, so a collision is a typo or a rebinding of
    a core client (which should reuse the existing entry, not re-declare it).
    """
    if spec.provider_key in _BY_PROVIDER:
        raise ValueError(
            f"provider plugin {plugin!r}: stop vocabulary key {spec.provider_key!r} already "
            "registered — bind the existing client class's entry instead of "
            "re-declaring it"
        )
    _BY_PROVIDER[spec.provider_key] = spec


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
