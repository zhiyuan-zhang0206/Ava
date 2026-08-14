"""ChatAnthropic subclass that surfaces thinking_tokens in usage_metadata.

langchain-anthropic's `_create_usage_metadata` drops `output_tokens_details`
from the Anthropic SDK `Usage` model — even though the model has
`output_tokens_details.thinking_tokens` (the reasoning token count, present for
every thinking-enabled call). Without it, `usage_metadata.output_token_details`
is always empty for all `ChatAnthropic`-routed models (claude-*, deepseek-*), so
`reasoning` stays 0 in observability.

`ThinkingTokensChatAnthropic` overrides the stream-chunk seam
(`_make_message_chunk_from_anthropic_event`) to capture `thinking_tokens` from
the raw `message_delta` event BEFORE `_create_usage_metadata` drops it, and
supplements the chunk's `usage_metadata.output_token_details.reasoning`.

This is the Anthropic-side mirror of `shared/lm/_reasoning_compat.py`
(`ReasoningContentChatModel` for ChatOpenAI).
"""

from __future__ import annotations

from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessageChunk


class ThinkingTokensChatAnthropic(ChatAnthropic):
    """ChatAnthropic that retains `thinking_tokens` in `usage_metadata`.

    Overrides the single chunk-conversion seam so downstream consumers
    (`agent.observe.log_llm_usage`, `agent.graph._llm._finalize_turn_observability`,
    `shared.timeline`, `gateway.routers.agent_inspect`) all see a non-zero
    `reasoning` count — no other file needs to change.
    """

    def _make_message_chunk_from_anthropic_event(
        self,
        event: Any,
        *,
        stream_usage: bool = True,
        coerce_content_to_string: bool = False,
        block_start_event: Any = None,
    ) -> tuple[AIMessageChunk | None, Any]:
        msg, block_start_event = super()._make_message_chunk_from_anthropic_event(
            event,
            stream_usage=stream_usage,
            coerce_content_to_string=coerce_content_to_string,
            block_start_event=block_start_event,
        )
        # The `message_delta` event carries the final usage including
        # output_tokens_details.thinking_tokens — which the upstream
        # _create_usage_metadata drops. Capture it here and supplement
        # the chunk's usage_metadata before it leaves our layer.
        if (
            msg is not None
            and msg.usage_metadata is not None
            and getattr(event, "type", None) == "message_delta"
        ):
            usage = event.usage
            output_details = getattr(usage, "output_tokens_details", None)
            if output_details is not None:
                thinking_tokens: int = getattr(output_details, "thinking_tokens", 0) or 0
                if thinking_tokens > 0:
                    existing = msg.usage_metadata.get("output_token_details") or {}
                    if "reasoning" not in existing and "reasoning_tokens" not in existing:
                        # Construct a fresh OutputTokenDetails dict (TypedDict,
                        # total=False) so the type checker is satisfied.
                        msg.usage_metadata["output_token_details"] = {
                            **existing,
                            "reasoning": thinking_tokens,
                        }
        return msg, block_start_event
