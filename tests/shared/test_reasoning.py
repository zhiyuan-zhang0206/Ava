"""shared/lm/reasoning.py — provider reasoning normalization.

`to_canonical_reasoning` folds every provider's reasoning block to the canonical
`{"type":"thinking","thinking":...}` shape so downstream consumers (streaming
fan-out, timeline, eval extractor) never branch on provider. Block shapes here
are captured from real provider astream output:
- anthropic / gemini: `{"type":"thinking","thinking":"..."}`
- openai Responses API: `{"type":"reasoning","summary":[{"type":"summary_text","text":"..."}]}`
"""

from __future__ import annotations

from typing import cast

from langchain_core.messages import UsageMetadata

from shared.lm.reasoning import (
    _count_thinking_chars,
    extract_reasoning_tokens,
    to_canonical_reasoning,
)


def test_openai_reasoning_block_folds_to_thinking():
    """openai `reasoning` block → `thinking`; visible text from summary[].text,
    index preserved (the content-block offset that places code items must not
    shift)."""
    out = to_canonical_reasoning(
        [
            {
                "type": "reasoning",
                "id": "rs_abc",
                "summary": [{"index": 0, "type": "summary_text", "text": "I weigh the options."}],
                "index": 0,
            }
        ]
    )
    assert out == [{"type": "thinking", "thinking": "I weigh the options.", "index": 0}]


def test_openai_reasoning_multiple_summary_parts_joined():
    """A long reasoning summary streams as several summary_text parts — joined
    in order into one thinking block."""
    out = to_canonical_reasoning(
        [
            {
                "type": "reasoning",
                "summary": [
                    {"index": 0, "type": "summary_text", "text": "First part. "},
                    {"index": 1, "type": "summary_text", "text": "Second part."},
                ],
                "index": 2,
            }
        ]
    )
    assert out == [{"type": "thinking", "thinking": "First part. Second part.", "index": 2}]


def test_openai_reasoning_empty_summary_folds_to_empty_thinking():
    """Boundary/initial reasoning chunk carries an empty summary — folds to an
    empty thinking block (consumers skip it like an anthropic signature-only
    block) but still keeps its index so the offset counts it."""
    out = to_canonical_reasoning([{"type": "reasoning", "summary": [], "index": 0}])
    assert out == [{"type": "thinking", "thinking": "", "index": 0}]


def test_reasoning_without_index_drops_index_key():
    """A reasoning block lacking `index` yields a thinking block without one —
    no synthetic index is invented."""
    out = to_canonical_reasoning([{"type": "reasoning", "summary": [{"text": "hi"}]}])
    assert out == [{"type": "thinking", "thinking": "hi"}]


def test_anthropic_thinking_block_passthrough():
    """anthropic/gemini already emit the canonical shape — left untouched."""
    block = {"type": "thinking", "thinking": "native thought", "index": 0}
    assert to_canonical_reasoning([block]) == [block]


def test_signature_only_thinking_passthrough():
    """anthropic signature_delta block (type=thinking, no text) is not a
    `reasoning` block — passes through unchanged; consumers skip it on the
    missing thinking text, not here."""
    block = {"type": "thinking", "signature": "opaque", "index": 0}
    assert to_canonical_reasoning([block]) == [block]


def test_text_and_tool_blocks_passthrough():
    """Non-reasoning blocks (text, openai function_call, anthropic tool_use)
    pass through verbatim."""
    blocks = [
        {"type": "text", "text": "hello", "index": 1},
        {"type": "function_call", "name": "execute_code", "arguments": "{}", "index": 2},
        {"type": "tool_use", "id": "tu_1", "name": "execute_code", "input": {}},
    ]
    assert to_canonical_reasoning(blocks) == blocks  # pyright: ignore[reportUnknownArgumentType]


def test_str_content_passthrough():
    """Bare-string content (legacy openai chat-completions / coerce path) is
    returned unchanged."""
    assert to_canonical_reasoning("plain reply") == "plain reply"


def test_non_dict_list_element_passthrough():
    """A non-dict list element (rare str-in-list) passes through."""
    assert to_canonical_reasoning(["raw", {"type": "reasoning", "summary": [{"text": "x"}]}]) == [
        "raw",
        {"type": "thinking", "thinking": "x"},
    ]


def test_input_not_mutated():
    """Stored state must stay provider-native (openai 400s on the next turn if
    its reasoning item is rewritten) — normalization returns a new list and
    never mutates the input blocks."""
    original = [{"type": "reasoning", "summary": [{"text": "keep native"}], "index": 0}]
    snapshot = [dict(b) for b in original]
    to_canonical_reasoning(original)
    assert original == snapshot


# ── extract_reasoning_tokens ──


def test_extract_from_output_token_details_reasoning():
    """Primary path: output_token_details.reasoning (Anthropic/DeepSeek)."""
    usage = cast(UsageMetadata, {"output_token_details": {"reasoning": 42}})
    assert extract_reasoning_tokens(usage) == 42


def test_extract_from_output_token_details_reasoning_tokens():
    """Primary path: output_token_details.reasoning_tokens (OpenAI, provider-extra key)."""
    usage = cast(UsageMetadata, {"output_token_details": {"reasoning_tokens": 99}})
    assert extract_reasoning_tokens(usage) == 99


def test_extract_fallback_to_content_thinking_blocks():
    """When usage_metadata has no output_token_details, fall back to counting
    thinking-block chars in message content."""
    content = [
        {"type": "thinking", "thinking": "This is a thought.", "index": 0},
        {"type": "text", "text": "Answer.", "index": 1},
    ]
    # usage_metadata present but no output_token_details → fallback
    usage = cast(UsageMetadata, {"input_tokens": 100, "output_tokens": 50})
    # 19 chars ÷ 3 ≈ 6 tokens
    assert extract_reasoning_tokens(usage, content=content) == max(1, 19 // 3)


def test_extract_fallback_to_content_reasoning_blocks():
    """OpenAI reasoning blocks (summary[].text) are counted too."""
    content = [
        {"type": "reasoning", "summary": [{"text": "Chain of thought."}], "index": 0},
    ]
    # usage_metadata present but no output_token_details → fallback
    usage = cast(UsageMetadata, {"input_tokens": 100, "output_tokens": 50})
    # "Chain of thought." = 17 chars ÷ 3 = 5
    assert extract_reasoning_tokens(usage, content=content) == max(1, 17 // 3)


def test_extract_total_reasoning_chars_takes_precedence():
    """When total_reasoning_chars is given, it wins over content scanning."""
    content = [{"type": "thinking", "thinking": "short"}]
    usage = cast(UsageMetadata, {"input_tokens": 100, "output_tokens": 50})
    # total_reasoning_chars=99 → 33 tokens, content=5 chars → 1 token
    assert extract_reasoning_tokens(usage, content=content, total_reasoning_chars=99) == 33


def test_extract_none_usage_returns_zero_unless_fallback():
    """None usage_metadata → no fallback (no LLM call happened)."""
    assert extract_reasoning_tokens(None) == 0
    # Even with thinking content, None usage means no LLM call
    content = [{"type": "thinking", "thinking": "some thought"}]
    assert extract_reasoning_tokens(None, content=content) == 0


def test_count_thinking_chars_empty():
    assert _count_thinking_chars([]) == 0


def test_count_thinking_chars_string():
    assert _count_thinking_chars("plain string") == 0


def test_count_thinking_chars_mixed_blocks():
    content = [
        {"type": "thinking", "thinking": "abc", "index": 0},
        {"type": "text", "text": "def", "index": 1},
        {"type": "thinking", "thinking": "ghi", "index": 2},
    ]
    assert _count_thinking_chars(content) == 6
