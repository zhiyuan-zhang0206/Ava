"""Chunk assembly + final-message validation for the llm node.

``_assemble_final_message`` folds the streamed ``AIMessageChunk`` list into one
``AIMessage`` via chunk addition, then runs the fail-fast validators:
``_sanitize_thinking_blocks`` (DeepSeek thinking-delta drift repair) and
``_validate_stop_reason`` (missing / truncated / unexpected terminal reason).

Split out of ``_llm.py`` (Task #1004 >800-line outlier) — a leaf dependency of
the node entry; nothing here imports back into ``_llm.py``.
"""

from __future__ import annotations

from typing import Any, cast

from langchain_core.messages import AIMessage, AIMessageChunk, message_chunk_to_message

from shared.lm.content import content_blocks
from shared.log import logger

from ._llm_errors import (
    LLMStreamCorruptedError,
    LLMStreamTruncatedError,
    LLMStreamUnexpectedStopReasonError,
)


def _sanitize_thinking_blocks(final_msg: AIMessage) -> None:
    """Repair thinking content blocks missing the `thinking` key (DeepSeek drift #167/168).

    DeepSeek's anthropic-compat endpoint intermittently drops the thinking_delta
    SSE event, so chunk accumulation yields `{"type":"thinking","signature":...}`
    with no `thinking`. Filling `thinking=""` repairs the wire shape: the filled
    block round-trips the endpoint (probed 2026-07-25: 200; the key-missing shape
    400s with "missing field `thinking`"), and every downstream consumer — the
    streaming fan-out (`agent/graph/_callbacks.py`), the timeline renderer
    (`shared/timeline.py`) — already skips empty-thinking blocks.

    Repair-in-place replaces the old fail-fast guard (raise → non-stream
    fallback): the drift is a permanent protocol quirk (~2% of deepseek turns),
    not a transient fault, so aborting the turn manufactured a doubled
    ~100K-token re-request plus a WARNING storm per hit. The sanitize INFO
    keeps the drift rate observable without counting as an operator alert.

    Scoped to `model_provider == 'anthropic'` (covers deepseek via
    ChatAnthropic) — openai/gemini reasoning is not Anthropic thinking blocks,
    so the guard early-returns for them. Within anthropic, only relevant when
    content is list (thinking + tool_use blocks); str content is plain text
    with no thinking blocks, skip.
    """
    if (final_msg.response_metadata or {}).get("model_provider") != "anthropic":  # pyright: ignore[reportUnknownMemberType]
        return
    content: Any = final_msg.content  # pyright: ignore[reportUnknownMemberType]
    if not isinstance(content, list):
        return
    for i, block in enumerate(content_blocks(cast(list[Any], content))):
        if not isinstance(block, dict) or block.get("type") != "thinking":
            continue
        if not isinstance(block.get("thinking"), str):
            logger.info(
                "thinking block [{block_idx}] missing 'thinking' field "
                "(signature={signature!r}) — signature_delta without thinking_delta; "
                "filled empty thinking (round-trip verified), turn continues",
                event="thinking_block_sanitized",
                block_idx=i,
                signature=block.get("signature", "<missing>"),
            )
            block["thinking"] = ""


# Terminal reasons that assert the model emitted a tool call: anthropic 'tool_use',
# openai 'tool_calls'. If one of these is set but no normalized tool_calls survived,
# the stream lost the tool block (bug 169) — fail-fast rather than idle.
_TOOL_CLAIMED_REASONS: frozenset[str] = frozenset({"tool_use", "tool_calls"})


def _validate_stop_reason(final_msg: AIMessage) -> None:
    """Fail-fast on a missing or abnormal terminal reason, normalized across providers.

    Uses `classify_stop` (dispatched on response_metadata['model_provider']) so
    anthropic `stop_reason`, openai/gemini `finish_reason`, and their differing
    vocabularies all map to one set of categories. Preserves the existing
    fail-fast contract: a missing terminal reason cannot be silently treated as
    idle (would let `if not final_msg.tool_calls` misjudge a lost final frame).
    """
    from shared.lm.stop import StopCategory, classify_stop

    category, raw = classify_stop(final_msg)
    output_tokens = (final_msg.usage_metadata or {}).get("output_tokens", 0)  # pyright: ignore[reportUnknownMemberType]
    if category is StopCategory.CORRUPTED:
        raise LLMStreamCorruptedError(
            f"final_msg terminal reason missing (metadata keys={list((final_msg.response_metadata or {}).keys())!r}). "  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            "Provider stream protocol drift / final frame lost — cannot determine whether the model stopped "
            "normally; cannot fallthrough and let if-not-tool_calls misjudge as idle."
        )
    # CORRUPTED is the only category where raw is None; after this point raw is always str.
    assert raw is not None  # noqa: S101 — narrowing: TRUNCATED/UNEXPECTED always have a raw value
    if category is StopCategory.TRUNCATED:
        raise LLMStreamTruncatedError(
            f"LLM stream truncated by server: terminal reason={raw!r} output_tokens={output_tokens}. "
            "Server output budget exhausted — usually client-side max_tokens too small. Abort turn; retry re-streams.",
            stop_reason=raw,
            output_tokens=output_tokens,
        )
    if category is StopCategory.UNEXPECTED:
        raise LLMStreamUnexpectedStopReasonError(
            f"LLM stream ended with unexpected terminal reason={raw!r} output_tokens={output_tokens}. "
            "Non-idle abnormal state (safety / content_filter / recitation etc.) — fail-fast so the user sees it.",
            stop_reason=raw,
            output_tokens=output_tokens,
        )
    # NORMAL. Consistency check: the terminal reason claims a tool call but no
    # normalized tool_calls survived = server emitted a tool intent but the block
    # was lost (bug 169). Covers anthropic 'tool_use' AND openai 'tool_calls'
    # (gemini uses 'STOP' for tool turns and is covered by .tool_calls routing).
    if raw in _TOOL_CLAIMED_REASONS and not final_msg.tool_calls:
        raise LLMStreamCorruptedError(
            f"terminal reason={raw!r} claims a tool call but final_msg.tool_calls is empty — "
            "server emitted a tool intent but no tool block in stream; cannot fallthrough as idle (bug 169)."
        )


def _assemble_final_message(chunks: list[AIMessageChunk]) -> AIMessage:
    """Fold streamed chunks into a single AIMessage + fail-fast validation.

    Accumulation via chunk addition merges content + usage_metadata;
    `message_chunk_to_message` converts to AIMessage. Then the guards:
    usage_metadata must exist at stream end (agent 113 metadata-loss
    incident), thinking blocks missing the `thinking` field are repaired in
    place (167/168 DeepSeek drift), and the terminal stop reason is validated
    (missing / truncated / unexpected → fail-fast).
    """
    accumulated = chunks[0]
    for c in chunks[1:]:
        accumulated = accumulated + c
    final_msg = message_chunk_to_message(accumulated)
    assert isinstance(final_msg, AIMessage)  # noqa: S101 — chunks from chat model are always AIMessage
    # fail-fast: agent 113 once hit "final_msg entered state with all metadata
    # empty" (additional_kwargs={}, usage_metadata=None, response_metadata={}),
    # but content + tool_calls intact. The next turn DeepSeek server 400'd
    # the process because of missing reasoning_content. At the time we could
    # not locate which path produced it (cancel? hook? saver?); adding this
    # assert is so the next collision raises immediately on site, traceback
    # pointing at the culprit. usage_metadata must exist at stream end
    # (providers emit it in the final chunk); missing = abnormal state
    # (agent 113 metadata-loss incident).
    _ak_keys = list(final_msg.additional_kwargs.keys())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    _rm_keys = list((final_msg.response_metadata or {}).keys())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    _content_len = len(final_msg.content) if isinstance(final_msg.content, str) else -1  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    _tool_calls_len = len(final_msg.tool_calls)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    assert final_msg.usage_metadata is not None, (  # noqa: S101
        f"final_msg.usage_metadata is None — abnormal state (additional_kwargs={_ak_keys}, "
        f"response_metadata={_rm_keys}, content_len={_content_len}, tool_calls={_tool_calls_len})"
    )
    # Repair thinking blocks missing the `thinking` field (167 / 168 DeepSeek
    # drift: signature_delta sent but thinking_delta missed) BEFORE final_msg
    # enters state.messages — the key-missing shape 400s the next turn's
    # request, while the filled `thinking=""` shape round-trips (probed
    # 2026-07-25). In-place repair keeps the turn's text/tool blocks instead
    # of aborting into a doubled re-request.
    _sanitize_thinking_blocks(final_msg)
    _validate_stop_reason(final_msg)
    return final_msg
