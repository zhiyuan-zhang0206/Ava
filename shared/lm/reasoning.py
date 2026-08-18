"""Provider-agnostic reasoning normalization — the seam above LangChain,
below the agent kernel.

Each provider carries its reasoning in a different content-block shape:

- anthropic (claude) / gemini: `{"type":"thinking", "thinking":"<text>"}`
- openai Responses API: `{"type":"reasoning", "summary":[{"type":"summary_text",
  "text":"<text>"}]}` — the visible text is nested in summary[].text

`to_canonical_reasoning` maps any of these to the single shape the timeline,
the streaming fan-out, and the eval extractor already speak — the `thinking`
block — so those consumers never branch on provider. Every other block (text,
tool_use, function_call, ...) passes through untouched.

This is a DISPLAY projection, not a rewrite of stored state. The committed
AIMessage must keep its provider-native blocks: openai 400s on the next turn
if its `reasoning` item is not echoed back verbatim alongside the matching
`function_call` (the same round-trip contract anthropic has for thinking +
signature). So callers normalize a *copy* for rendering and leave the stored
message native.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import UsageMetadata

from shared.lm.content import ContentBlock, content_blocks


def _canonical_block(block: ContentBlock) -> ContentBlock:
    """Map one openai Responses `reasoning` block to a `thinking` block, or
    return `block` unchanged.

    The visible text is the concatenation of summary[].text. `index` is
    preserved so the content-block offset that places tool-call code items is
    unchanged. A reasoning block with no summary text (the boundary/initial
    chunk) maps to an empty thinking block, which consumers skip exactly like
    an anthropic signature-only block.
    """
    if block.get("type") != "reasoning":
        return block
    text = "".join(
        part.get("text", "") for part in (block.get("summary") or []) if isinstance(part, dict)
    )
    out: ContentBlock = {"type": "thinking", "thinking": text}
    if "index" in block:
        out["index"] = block["index"]
    return out


def to_canonical_reasoning(content: str | list[Any]) -> str | list[str | ContentBlock]:
    """Normalize provider-native reasoning blocks to the canonical `thinking`
    shape.

    `content` is an AIMessage(Chunk).content: a string (returned unchanged) or
    a list of blocks (returns a new list — reasoning blocks normalized, every
    other element, dict or str, passed through unchanged). The input is never
    mutated; the stored message stays provider-native.
    """
    if not isinstance(content, list):
        return content
    return [_canonical_block(b) if isinstance(b, dict) else b for b in content_blocks(content)]


def extract_reasoning_tokens(
    usage_metadata: UsageMetadata | None,
    *,
    content: str | list[Any] | None = None,
    total_reasoning_chars: int = 0,
) -> int:
    """Extract reasoning token count from usage_metadata, with content fallback.

    Primary: read ``output_token_details.reasoning`` (Anthropic/DeepSeek via
    ThinkingTokensChatAnthropic) or ``output_token_details.reasoning_tokens``
    (OpenAI). If neither is present, fall back to estimating from the visible
    thinking content.

    Args:
        usage_metadata: The AIMessage's usage_metadata dict (or None).
        content: The message's content — a list of content blocks or a
            string. Used as a fallback to count thinking-block characters.
        total_reasoning_chars: Pre-counted total chars of streamed thinking
            text (from RedisStreamHandler). More accurate than scanning
            content, because the handler sees every fragment before any
            truncation. Takes precedence over content scanning.

    Returns:
        Non-negative integer reasoning token count.
    """
    # Primary: extract from usage_metadata.output_token_details
    if usage_metadata:
        details = usage_metadata.get("output_token_details") or {}
        from_details = details.get("reasoning") or details.get("reasoning_tokens")
        if from_details:
            return int(from_details)

        # Fallback: usage_metadata is present (LLM call happened) but
        # output_token_details is absent. Estimate from thinking content.
        reasoning_chars = total_reasoning_chars
        if reasoning_chars == 0 and content is not None:
            reasoning_chars = _count_thinking_chars(content)
        if reasoning_chars > 0:
            # Rough token estimate: English ~4 chars/token, CJK ~1.5 chars/token.
            # A mixed ratio of 3 chars/token is a reasonable default.
            return max(1, reasoning_chars // 3)
    return 0


def _count_thinking_chars(content: str | list[Any]) -> int:
    """Count total characters in thinking/reasoning blocks of a message content."""
    if isinstance(content, str):
        return 0  # string content has no block-type metadata
    total = 0
    for block in content_blocks(content):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "thinking":
            total += len(block.get("thinking") or "")
        elif btype == "reasoning":
            # OpenAI Responses reasoning block: text in summary[].text
            for part in block.get("summary") or []:
                if isinstance(part, dict):
                    total += len(part.get("text") or "")
    return total
