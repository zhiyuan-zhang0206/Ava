"""The compaction LLM call behind a summary, kept so the boundary checkpoint can record it."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from base.agents.history.closing_request import ClosingRequest


class SummaryText(str):
    """A compaction summary that remembers the LLM call which produced it.

    `closing` is that call as a `ClosingRequest` (its provider input tokens, model and the
    instruction's size), what `stamp_compact_boundary` writes into the boundary checkpoint so the
    sealed segment's tail can be priced. A plain `str` summary (agent-written, the no-LLM
    fallback) has none. It is a `str` so every consumer of the summary text is unchanged.
    """

    closing: ClosingRequest | None

    def __new__(cls, text: str, closing: ClosingRequest | None = None) -> SummaryText:
        obj = super().__new__(cls, text)
        obj.closing = closing
        return obj


def closing_of(summary: str) -> ClosingRequest | None:
    """The LLM call behind `summary`, when it came from one."""
    return summary.closing if isinstance(summary, SummaryText) else None


def closing_request_of(response: AIMessage, instruction: HumanMessage) -> ClosingRequest | None:
    """The compaction call's provider-reported input, as the anchor of the segment it read.
    None when the provider reported no usage."""
    from base.agents.messages.token_estimate import estimate_message_tokens

    usage = response.usage_metadata
    if not usage or not usage.get("input_tokens"):
        return None
    meta = response.response_metadata
    model = meta.get("model_name") or meta.get("model")
    return ClosingRequest(
        input_tokens=usage["input_tokens"],
        extra_tokens=round(estimate_message_tokens(instruction)),
        model=model if isinstance(model, str) and model else None,
    )
