"""Unit tests for `_sanitize_thinking_blocks` — repair for the 167/168 DeepSeek drift.

DeepSeek's anthropic-compatible endpoint intermittently misses the thinking_delta SSE event
(only sends signature_delta + content_block_stop), so chunk accumulation yields
`{"type": "thinking", "signature": "...", "index": 0}` with no `thinking` key.
Sending that key-missing shape back next turn 400s ("missing field `thinking`");
the filled `thinking=""` shape round-trips (probed against the live endpoint
2026-07-25: 200).

The old fail-fast guard aborted the whole turn (raise → non-stream fallback →
doubled ~100K-token re-request) over a permanent protocol quirk (~2% of deepseek
turns, per the 2026-07 cluster-log sweep). The sanitizer repairs the block in
place: the turn keeps its text/tool blocks, and the WARNING keeps the drift
rate observable.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from agent.graph._llm import _sanitize_thinking_blocks

_ANTHROPIC_META = {"model_provider": "anthropic", "stop_reason": "end_turn"}


def test_sanitize_leaves_complete_thinking_block_untouched() -> None:
    """Normal thinking block (thinking + signature + type) → not modified, no WARNING."""
    msg = AIMessage(
        content=[
            {
                "type": "thinking",
                "thinking": "Let me reason about this...",
                "signature": "sig-abc",
                "index": 0,
            },
            {"type": "tool_use", "id": "call_1", "name": "execute_code", "input": {}, "index": 1},
        ],
        response_metadata=_ANTHROPIC_META,
    )
    _sanitize_thinking_blocks(msg)
    block = msg.content[0]  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(block, dict)
    assert block["thinking"] == "Let me reason about this..."
    assert block["signature"] == "sig-abc"


def test_sanitize_fills_missing_thinking_and_preserves_signature() -> None:
    """Signature-only thinking block (167/168 drift shape) → `thinking` filled with "",
    signature + index + sibling blocks preserved, no raise — the repaired message is the
    round-trip-verified shape that must enter state.messages."""
    msg = AIMessage(
        content=[
            {
                "type": "thinking",
                # missing 'thinking' field, but signature_delta was sent
                "signature": "sig-orphan-167",
                "index": 0,
            },
            {"type": "tool_use", "id": "call_1", "name": "execute_code", "input": {}, "index": 1},
        ],
        response_metadata=_ANTHROPIC_META,
    )
    _sanitize_thinking_blocks(msg)  # does not raise
    block = msg.content[0]  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(block, dict)
    assert block["thinking"] == ""
    assert block["signature"] == "sig-orphan-167"
    assert block["index"] == 0
    # sibling tool_use block untouched
    assert msg.content[1] == {  # pyright: ignore[reportUnknownMemberType]
        "type": "tool_use",
        "id": "call_1",
        "name": "execute_code",
        "input": {},
        "index": 1,
    }


def test_sanitize_logs_signature_for_diagnosis(loguru_records) -> None:
    """The sanitize INFO carries the signature — server-side logs can grep it to
    locate which stream drifted; the drift rate stays observable after the abort
    is gone (INFO, not WARNING: a routine repair, not an operator alert)."""
    msg = AIMessage(
        content=[
            {"type": "thinking", "signature": "sig-orphan-167", "index": 0},
        ],
        response_metadata=_ANTHROPIC_META,
    )
    _sanitize_thinking_blocks(msg)
    infos = [r for r in loguru_records if r["level"].name == "INFO"]  # pyright: ignore[reportUnknownMemberType]
    assert len(infos) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert "sig-orphan-167" in infos[0]["message"]
    assert "thinking block [0]" in infos[0]["message"]


def test_sanitize_logs_missing_placeholder_without_signature(loguru_records) -> None:
    """thinking block missing both 'thinking' and 'signature' → the INFO renders the
    `'<missing>'` placeholder (`block.get('signature', '<missing>')`)."""
    msg = AIMessage(
        content=[{"type": "thinking"}],
        response_metadata=_ANTHROPIC_META,
    )
    _sanitize_thinking_blocks(msg)
    infos = [r for r in loguru_records if r["level"].name == "INFO"]  # pyright: ignore[reportUnknownMemberType]
    assert len(infos) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert "'<missing>'" in infos[0]["message"]
    block = msg.content[0]  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(block, dict)
    assert block["thinking"] == ""


def test_sanitize_skips_string_content() -> None:
    """Non-thinking model (pure text output) content is str, no blocks to repair."""
    msg = AIMessage(content="just a plain text response, no thinking blocks")
    _sanitize_thinking_blocks(msg)  # does not raise
    assert msg.content == "just a plain text response, no thinking blocks"  # pyright: ignore[reportUnknownMemberType]


def test_sanitize_skips_non_thinking_blocks() -> None:
    """text / tool_use blocks are not thinking → left untouched, no WARNING."""
    msg = AIMessage(
        content=[
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "x", "name": "execute_code", "input": {}, "index": 1},
        ]
    )
    _sanitize_thinking_blocks(msg)
    assert msg.content[0] == {"type": "text", "text": "hello"}  # pyright: ignore[reportUnknownMemberType]


def test_sanitize_repairs_every_incomplete_block() -> None:
    """Every thinking block missing the field is repaired (loop does not stop at the
    first): block[1] keeps its text, block[2] gets filled."""
    msg = AIMessage(
        content=[
            {"type": "text", "text": "intro"},  # non-thinking: skipped, loop continues
            {"type": "thinking", "thinking": "ok block", "signature": "sig-a", "index": 1},
            {"type": "thinking", "signature": "sig-b", "index": 2},  # missing thinking
        ],
        response_metadata=_ANTHROPIC_META,
    )
    _sanitize_thinking_blocks(msg)
    ok_block, repaired = msg.content[1], msg.content[2]  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(ok_block, dict) and isinstance(repaired, dict)
    assert ok_block["thinking"] == "ok block"
    assert repaired["thinking"] == ""
    assert repaired["signature"] == "sig-b"


def test_sanitize_fills_non_string_thinking() -> None:
    """A `thinking: None` value is as wire-invalid as a missing key (null would 400 the
    strict schema the same way) → filled with "" too."""
    msg = AIMessage(
        content=[{"type": "thinking", "thinking": None, "signature": "sig-null"}],
        response_metadata=_ANTHROPIC_META,
    )
    _sanitize_thinking_blocks(msg)
    block = msg.content[0]  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(block, dict)
    assert block["thinking"] == ""


def test_sanitize_skips_non_anthropic_provider() -> None:
    """Non-anthropic provider content is not the Anthropic thinking-block schema — the
    guard must not judge (or mutate) it."""
    msg = AIMessage(
        content=[{"type": "thinking", "signature": "abc"}],  # missing 'thinking' key
        response_metadata={"model_provider": "google_genai", "finish_reason": "STOP"},
    )
    _sanitize_thinking_blocks(msg)
    # untouched — still no 'thinking' key
    block = msg.content[0]  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(block, dict)
    assert "thinking" not in block
