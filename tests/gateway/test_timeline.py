"""Contract tests for GET /api/agents/{id}/timeline.

Design (2026-05-04 in-place simplification):
  - LangGraph state.messages is the sole source of truth
  - The inbound_messages table does **not** directly enter the timeline (it is already
    envelope-wrapped into LangGraph state), only used as a ts anchor
  - AIMessage.content (agent text) / tool_call code / reasoning are all rendered from
    state; adjacent timestamps within the same LLM turn no longer drift apart

Coverage:
  - endpoint basic contract (404 / empty agent / no orphaned inbounds displayed /
    lifecycle anchor filtering)
  - `_ai_message_items` helper unit (TestAiMessageItems): directly feed AIMessage to test
    block splitting
  - dispatch end-to-end (TestTimelineDispatch): truly load mixed state.messages into
    PostgresSaver checkpoint, run a full GET /timeline, verify that AIMessage / lifecycle /
    string-content paths all correctly render through the dispatch chain — unit test helpers
    passing ≠ dispatch routing correct (the bug in refactor 8a3c520 that accidentally deleted
    the elif header and silently lost AIMessage is exactly this kind of bug)
"""

from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import BaseMessage, HumanMessage

from gateway.app import app
from gateway.routers.timeline import _window_before
from shared.db import create_agent, insert_inbound_message
from shared.timeline import TimelineItem, _ai_message_items, build_timeline_items, tail_window


@pytest.fixture
def test_client(db_conn: psycopg.Connection):
    """TestClient + lifespan. db_conn's TRUNCATE runs before the lifespan —
    the DB pool reuses the same test library (settings.data_plane.db_url is
    already replaced by conftest)."""
    with TestClient(app) as client:
        yield client


def test_timeline_404_for_unknown_thread(test_client: TestClient) -> None:
    resp = test_client.get("/api/agents/99999/timeline")
    assert resp.status_code == 404


def test_timeline_empty_for_new_agent(db_conn: psycopg.Connection, test_client: TestClient) -> None:
    """New agent with no LangGraph state → returns empty list."""
    tid = create_agent(db_conn)
    resp = test_client.get(f"/api/agents/{tid}/timeline")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "msg_count": 0, "has_more": False}


class TestAiMessageItems:
    """`_ai_message_items(msg, msg_idx, next_ts)` splits a single AIMessage into
    per-block timeline items, each carrying a stable `item_id = f"{msg_idx}.{block_idx}"`.

    The production path ChatAnthropic + bind_tools returns list-of-blocks; legacy /
    no-tools degenerates to string content (the whole thing as chat, block_idx=0).
    """

    @staticmethod
    def _next_ts(_msg=None):
        # In production, next_ts returns the message's own real timestamp (ava_created_at)
        # or an incrementing-microsecond fallback; these block-split tests don't care about
        # timestamps, so the stub ignores _msg and returns a placeholder value.
        return "2026-01-01T00:00:00.000001+00:00"

    def _items(self, msg, msg_idx=5):
        from langchain_core.messages import AIMessage

        if not isinstance(msg, AIMessage):
            msg = AIMessage(content=msg)  # pyright: ignore[reportUnknownArgumentType]
        return _ai_message_items(msg, msg_idx, self._next_ts)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    def test_string_content_treated_as_chat(self):
        from langchain_core.messages import AIMessage

        items = self._items(AIMessage(content="hello world"))  # pyright: ignore[reportUnknownMemberType]
        assert len(items) == 1
        assert items[0].kind == "agent_chat"
        assert items[0].payload == "hello world"
        assert items[0].item_id == "5.0"

    def test_empty_string_content_no_item(self):
        from langchain_core.messages import AIMessage

        items = self._items(AIMessage(content=""))  # pyright: ignore[reportUnknownMemberType]
        assert items == []

    def test_openai_string_content_plus_tool_call_renders_code(self):
        """openai (gpt-*) shape: content is a string (chat), tool call only in tool_calls.
        chat lands at 5.0, code offset to 5.1."""
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content="Hi!",
            tool_calls=[{"name": "execute_code", "args": {"code": "go()"}, "id": "o0"}],
        )
        items = self._items(msg)  # pyright: ignore[reportUnknownMemberType]
        assert [(it.kind, it.payload, it.item_id) for it in items] == [
            ("agent_chat", "Hi!", "5.0"),
            ("agent_code", "go()", "5.1"),
        ]

    def test_thinking_then_text_blocks(self):
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content=[
                {"type": "thinking", "thinking": "let me consider", "index": 0},
                {"type": "text", "text": "the answer is 42", "index": 1},
            ]
        )
        items = self._items(msg)  # pyright: ignore[reportUnknownMemberType]
        assert [(it.kind, it.payload, it.item_id) for it in items] == [
            ("agent_reasoning", "let me consider", "5.0"),
            ("agent_chat", "the answer is 42", "5.1"),
        ]

    def test_openai_reasoning_block_renders_agent_reasoning(self):
        """openai Responses committed shape: a `reasoning` block (text in
        summary[].text) + text block + `function_call` block, tool call also in
        the normalized tool_calls. shared.lm.reasoning folds reasoning → thinking,
        so it renders as agent_reasoning; function_call is skipped (code comes
        from tool_calls); offsets: reasoning@0, chat@1, code@2.
        Shape taken from a real gpt-5.4-mini Responses committed message."""
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "summary": [{"index": 0, "type": "summary_text", "text": "weigh it"}],
                    "index": 0,
                },
                {"type": "text", "text": "Day 8.", "index": 1},
                {"type": "function_call", "name": "execute_code", "arguments": "{}", "index": 2},
            ],
            tool_calls=[{"name": "execute_code", "args": {"code": "print(8)"}, "id": "o0"}],
        )
        items = self._items(msg)  # pyright: ignore[reportUnknownMemberType]
        assert [(it.kind, it.payload, it.item_id) for it in items] == [
            ("agent_reasoning", "weigh it", "5.0"),
            ("agent_chat", "Day 8.", "5.1"),
            ("agent_code", "print(8)", "5.2"),
        ]

    def test_reasoning_ms_and_tokens_on_first_thinking_item(self):
        """ava_reasoning_ms_by_block (persisted by llm_node) gives the thinking
        block its per-block duration; usage_metadata reasoning tokens land on
        the first thinking item; other items keep them None."""
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content=[
                {"type": "thinking", "thinking": "ponder", "index": 0},
                {"type": "text", "text": "done", "index": 1},
            ],
            additional_kwargs={"ava_reasoning_ms_by_block": {"0": 8200}},
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "output_token_details": {"reasoning": 1234},
            },
        )
        items = self._items(msg)  # pyright: ignore[reportUnknownMemberType]
        reasoning = items[0]
        assert reasoning.kind == "agent_reasoning"
        assert reasoning.reasoning_ms == 8200
        assert reasoning.reasoning_tokens == 1234
        # text item carries neither
        assert items[1].reasoning_ms is None
        assert items[1].reasoning_tokens is None

    def test_per_block_reasoning_ms_each_thinking_block(self):
        """Two thinking blocks in one turn each carry their OWN per-block
        duration (keyed by block_idx), so an interleaved-thinking model is timed
        block by block, not as one turn-spanning total. Tokens stay turn-level
        on the first block (usage_metadata reports one total)."""
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content=[
                {"type": "thinking", "thinking": "first", "index": 0},
                {"type": "thinking", "thinking": "second", "index": 1},
            ],
            additional_kwargs={"ava_reasoning_ms_by_block": {"0": 3000, "1": 1500}},
            usage_metadata={
                "input_tokens": 10,
                "output_tokens": 20,
                "total_tokens": 30,
                "output_token_details": {"reasoning": 500},
            },
        )
        items = self._items(msg)  # pyright: ignore[reportUnknownMemberType]
        assert (items[0].reasoning_ms, items[0].reasoning_tokens) == (3000, 500)
        assert (items[1].reasoning_ms, items[1].reasoning_tokens) == (1500, None)

    def test_legacy_single_reasoning_ms_falls_back_to_first_block(self):
        """Turns persisted before per-block timing carry a single turn-level
        `ava_reasoning_ms`; it is read back onto the first thinking block only,
        so old timelines still render 'Thought for X'."""
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content=[
                {"type": "thinking", "thinking": "first", "index": 0},
                {"type": "thinking", "thinking": "second", "index": 1},
            ],
            additional_kwargs={"ava_reasoning_ms": 3000},
        )
        items = self._items(msg)  # pyright: ignore[reportUnknownMemberType]
        assert items[0].reasoning_ms == 3000
        assert items[1].reasoning_ms is None

    def test_no_reasoning_metadata_leaves_fields_none(self):
        """A plain thinking block with no persisted ms / no usage details keeps
        both summary fields None (e.g. historical checkpoint before the field existed)."""
        from langchain_core.messages import AIMessage

        msg = AIMessage(content=[{"type": "thinking", "thinking": "hmm", "index": 0}])
        items = self._items(msg)  # pyright: ignore[reportUnknownMemberType]
        assert items[0].reasoning_ms is None
        assert items[0].reasoning_tokens is None

    def test_exec_ms_surfaces_on_code_output(self):
        """ava_exec_ms (the exec wall-clock stashed by the exec node) lands on
        the code_output item so the collapsed chip can read 'ran in Xs'."""
        from agent.messages import exec_output_message
        from shared.timeline import build_timeline_items

        msg = exec_output_message(content="hello", tool_call_id="t1", exit_code=0, exec_ms=1300)
        items, _ = build_timeline_items([msg], [])
        assert len(items) == 1
        assert items[0].kind == "code_output"
        assert items[0].exec_ms == 1300

    def test_exec_ms_absent_leaves_field_none(self):
        """A historical exec_output checkpoint without ava_exec_ms keeps exec_ms
        None — the chip then just shows the line count."""
        from langchain_core.messages import ToolMessage

        from shared.timeline import build_timeline_items

        msg = ToolMessage(
            content="hello",
            tool_call_id="t1",
            additional_kwargs={"ava_msg_type": "exec_output"},
        )
        items, _ = build_timeline_items([msg], [])
        assert items[0].kind == "code_output"
        assert items[0].exec_ms is None

    def test_system_note_renders_as_system_marker(self):
        """A framework-injected system_note (e.g. an SDK-nudge hint) renders as
        a system_marker carrying source=<note tag> — the discriminator the
        frontend dispatches on to pick a chip. Asserting the source (not just
        the kind) is what fails if the note tag stops flowing through: the
        HumanMessage catch-all would still produce a system_marker, but with
        source=None (the red UnknownMarkerChip path)."""
        from agent.messages import NoteTag, system_note_message
        from shared.timeline import build_timeline_items

        msg = system_note_message(content="use ava.agents.send_message", tag=NoteTag.AGENT_REPLY)
        items, _ = build_timeline_items([msg], [])
        assert len(items) == 1
        assert items[0].kind == "system_marker"
        assert items[0].source == "agent_reply"
        assert items[0].payload == "[system] use ava.agents.send_message"

    def test_system_note_show_timestamp_by_tag(self):
        """system_marker.show_timestamp splits notes into events (heartbeat +
        lifecycle_*, ts shown) vs standing context / guidance nudges (memory +
        the one-time hints, ts hidden). The frontend chip reads this flag to
        decide whether to render the wall-clock ts."""
        from agent.messages import NoteTag, system_note_message
        from shared.timeline import build_timeline_items

        shown = {
            NoteTag.HEARTBEAT,
            NoteTag.LIFECYCLE_TERMINATE,
            NoteTag.LIFECYCLE_RESTART,
            NoteTag.LIFECYCLE_RESURRECT,
            NoteTag.LIFECYCLE_FORK,
            # A skill appearing mid-window is something that happened at a
            # moment, unlike the standing listing it amends.
            NoteTag.NEW_SKILLS,
            # A task notification is an event (the assignment / update /
            # reminder happened at a moment) — the wall clock belongs.
            NoteTag.TASK,
        }
        hidden = {
            NoteTag.MEMORY,
            NoteTag.AGENT_ID,
            NoteTag.COMPACT_REMINDER,
            NoteTag.HISTORY_DUMP,
            NoteTag.SDK_HINT,
            NoteTag.AGENT_REPLY,
            NoteTag.SILENT_IDLE_CONTINUE,
            NoteTag.SECURITY,
            # Historical heartbeat-pause notes are one-time guidance, same
            # family as the security notes beside them.
            NoteTag.HEARTBEAT_PAUSE,
            NoteTag.CONTEXT,
            NoteTag.AGENT_ID,
            NoteTag.PROJECT_SKILLS,
            NoteTag.PRELOADED_SKILLS,
            NoteTag.AGENT_MEMORY,
            NoteTag.EXEC_TIMEOUT,
            # Standing head content, like the exec timeout beside it: its ts
            # would only say when the window opened, and the note's whole point
            # is that the timezone does not change.
            NoteTag.TIMEZONE,
        }
        # Every NoteTag is classified — a new member added without a decision
        # here fails this, forcing the show/hide call to be made explicitly.
        assert shown | hidden == set(NoteTag)

        for tag in shown:
            items, _ = build_timeline_items([system_note_message(content="x", tag=tag)], [])
            assert items[0].show_timestamp is True, tag
        for tag in hidden:
            items, _ = build_timeline_items([system_note_message(content="x", tag=tag)], [])
            assert items[0].show_timestamp is False, tag

    def test_system_message_renders_as_system_prompt(self):
        """The agent's system prompt (state.messages[0], a SystemMessage) renders
        as a `system_prompt` item with created_at None — it has no inbound anchor
        and is always the first item, so it carries no timestamp."""
        from langchain_core.messages import SystemMessage

        from shared.timeline import build_timeline_items

        msg = SystemMessage(content="You are Ava.\nAct via execute_code.")
        items, msg_count = build_timeline_items([msg], [])
        assert msg_count == 1
        assert len(items) == 1
        assert items[0].kind == "system_prompt"
        assert items[0].item_id == "0.0"
        assert items[0].payload == "You are Ava.\nAct via execute_code."
        assert items[0].created_at is None
        assert items[0].source is None

    def test_signature_only_thinking_block_skipped(self):
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content=[
                {"type": "thinking", "thinking": "real thought", "index": 0},
                {"type": "thinking", "signature": "opaque-bytes", "index": 0},
            ]
        )
        items = self._items(msg)  # pyright: ignore[reportUnknownMemberType]
        assert [(it.kind, it.payload) for it in items] == [
            ("agent_reasoning", "real thought"),
        ]

    def test_redacted_thinking_skipped(self):
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content=[
                {"type": "redacted_thinking", "data": "encrypted", "index": 0},
                {"type": "text", "text": "visible", "index": 1},
            ]
        )
        items = self._items(msg)  # pyright: ignore[reportUnknownMemberType]
        assert [(it.kind, it.payload, it.item_id) for it in items] == [
            ("agent_chat", "visible", "5.1"),
        ]

    def test_tool_use_block_pulls_code_from_tool_calls(self):
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content=[
                {"type": "text", "text": "running tool", "index": 0},
                {
                    "type": "tool_use",
                    "id": "call_x",
                    "name": "execute_code",
                    "input": {"code": "print('hi')"},
                    "index": 1,
                },
            ],
            tool_calls=[
                {
                    "name": "execute_code",
                    "args": {"code": "print('hi')"},
                    "id": "call_x",
                }
            ],
        )
        items = self._items(msg)  # pyright: ignore[reportUnknownMemberType]
        assert [(it.kind, it.payload, it.item_id) for it in items] == [
            ("agent_chat", "running tool", "5.0"),
            ("agent_code", "print('hi')", "5.1"),
        ]

    def test_gemini_tool_call_not_in_content_still_renders_code(self):
        """gemini / openai shape: tool call only in tool_calls, content has no tool_use
        block. code still renders from tool_calls, placed after the text block (5.1) without
        colliding with text's 5.0."""
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content=[{"type": "text", "text": "Let me compute", "index": 0}],
            tool_calls=[{"name": "execute_code", "args": {"code": "print(2 + 3)"}, "id": "g0"}],
        )
        items = self._items(msg)  # pyright: ignore[reportUnknownMemberType]
        assert [(it.kind, it.payload, it.item_id) for it in items] == [
            ("agent_chat", "Let me compute", "5.0"),
            ("agent_code", "print(2 + 3)", "5.1"),
        ]

    def test_gemini_multiple_tool_calls_render_distinct_code_blocks(self):
        """gemini with multiple tool calls (no tool_use in content) → each gets its own
        independent code item, block_idx generated sequentially after the content blocks
        (0/1/2 when no text)."""
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content=[],
            tool_calls=[
                {"name": "execute_code", "args": {"code": "print(1)"}, "id": "c0"},
                {"name": "execute_code", "args": {"code": "print(2)"}, "id": "c1"},
                {"name": "execute_code", "args": {"code": "print(3)"}, "id": "c2"},
            ],
        )
        items = self._items(msg)  # pyright: ignore[reportUnknownMemberType]
        assert [(it.kind, it.payload, it.item_id) for it in items] == [
            ("agent_code", "print(1)", "5.0"),
            ("agent_code", "print(2)", "5.1"),
            ("agent_code", "print(3)", "5.2"),
        ]

    def test_per_block_items_not_aggregated(self):
        """thinking + text + thinking becomes three items, no longer merged into two
        buckets as the old _extract did — block_idx is 1:1 with anthropic
        content_block_index; frontend merge uses the same id hit."""
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content=[
                {"type": "thinking", "thinking": "first", "index": 0},
                {"type": "text", "text": "talk", "index": 1},
                {"type": "thinking", "thinking": "second", "index": 2},
            ]
        )
        items = self._items(msg)  # pyright: ignore[reportUnknownMemberType]
        assert [(it.kind, it.payload, it.item_id) for it in items] == [
            ("agent_reasoning", "first", "5.0"),
            ("agent_chat", "talk", "5.1"),
            ("agent_reasoning", "second", "5.2"),
        ]

    def test_empty_list_no_items(self):
        from langchain_core.messages import AIMessage

        items = self._items(AIMessage(content=[]))  # pyright: ignore[reportUnknownMemberType]
        assert items == []


class TestAvaMsgTypeDispatch:
    """Every AvaMsgType member must dispatch to its intended timeline item kind.

    A HumanMessage tagged with a known ava_msg_type that the render loop has no
    branch for falls to `_fallback_human_item` — a system_marker with
    source=None that the frontend renders as the red "unrecognized" alarm
    (the #1017 regression class: the auto-compact path produced an untagged
    summary). This parametrized test makes "add a new AvaMsgType without
    adapting the renderer" impossible: every member must render as its
    intended kind, never the null-source catch-all.

    Companion cross-stack contract: tests/test_lint_marker_contract.py
    (backend NoteTag ⊆ frontend dispatch sets) + the frontend marker-contract
    tests in ui/web/src/components/timeline.test.tsx.
    """

    @staticmethod
    def _render(msg: HumanMessage) -> list[TimelineItem]:
        from shared.timeline import build_timeline_items

        return build_timeline_items([msg], [])[0]

    @staticmethod
    def _tagged(msg_type: str, **extra: object) -> HumanMessage:
        return HumanMessage(
            content="payload",
            additional_kwargs={"ava_msg_type": msg_type, **extra},
        )

    def test_every_msg_type_has_an_explicit_branch(self) -> None:
        from shared.message_kwargs import AvaMsgType

        expected_kind = {
            AvaMsgType.INBOUND: "inbound_chat",
            AvaMsgType.EXEC_OUTPUT: "code_output",
            AvaMsgType.SYSTEM_NOTE: "system_marker",
            AvaMsgType.COMPACT_SUMMARY: "inbound_compact_summary",
            AvaMsgType.COMPACT_REQUEST: "inbound_compact_request",
        }
        # SYSTEM_NOTE needs a note_tag to render as its card chip (not the
        # null-source historical-data alarm path).
        note_tag = {"ava_note_tag": "sdk_hint"}
        by_type = {AvaMsgType.SYSTEM_NOTE: note_tag}
        for msg_type, expected in expected_kind.items():
            items = self._render(self._tagged(msg_type.value, **by_type.get(msg_type, {})))
            assert len(items) == 1, f"{msg_type}: expected exactly one item, got {items}"
            item = items[0]
            assert item.kind == expected, (
                f"AvaMsgType.{msg_type.name} renders as {item.kind!r} (source={item.source!r}), "
                f"expected {expected!r} — the render loop needs an explicit branch for it"
            )
            # A tagged message must NEVER hit the catch-all (system_marker +
            # source=None). SYSTEM_NOTE with a tag is the card path; without a
            # tag it is deliberately fail-loud historical data, so only assert
            # source on the tagged SYSTEM_NOTE construction.
            if item.kind == "system_marker":
                assert item.source is not None, (
                    f"AvaMsgType.{msg_type.name} fell to the null-source catch-all: {item}"
                )

    def test_untagged_human_message_hits_catch_all_fail_loud(self) -> None:
        """Untagged framework messages DO hit the catch-all — that is the
        fail-loud design (a red chip beats silent mis-rendering). Compact
        paths are asserted by the agent-side tests to never produce one."""
        items = self._render(HumanMessage(content="legacy untagged note"))
        assert len(items) == 1
        assert items[0].kind == "system_marker"
        assert items[0].source is None


class TestAttachItems:
    """`build_timeline_items` dispatch for ava_msg_type="attach" messages.

    An attach message (product of `agent/graph/_attach_drain.py`) carries a
    leading text caption block plus provider-native media blocks (image data
    URIs, pdf document blocks, ...). It must render as a dedicated `attach`
    item: payload = caption text only (the base64 must never leak into the
    payload), images = the image data URIs for thumbnails, item_id = msg_idx.0.
    """

    @staticmethod
    def _attach_message(
        tmp_path: Path,
        *,
        blocks_override: list[dict[str, Any]] | None = None,
    ) -> HumanMessage:
        from agent.messages import attach_message
        from shared.lm.attach import AttachEntry, pack_attachments

        image = tmp_path / "render.png"
        from PIL import Image

        Image.new("RGB", (1, 1)).save(image)
        pack = pack_attachments(
            "deepseek-v4-flash-vision-exp",
            [AttachEntry(path=str(image.resolve()), label="after fix")],
        )
        assert pack is not None
        blocks = blocks_override if blocks_override is not None else pack.blocks
        from datetime import UTC, datetime

        return attach_message(blocks=blocks, text=pack.text, created_at=datetime.now(UTC))

    def test_attach_renders_caption_only_with_image_data_uris(self, tmp_path: Path):
        msg = self._attach_message(tmp_path)
        items, _ = build_timeline_items([msg], [])
        assert len(items) == 1
        item = items[0]
        assert item.kind == "attach"
        assert item.source is None
        assert item.item_id == "0.0"
        assert item.images is not None and len(item.images) == 1
        assert item.images[0].startswith("data:image/png;base64,")
        # The image's own caption line rides beside the thumbnail (1:1 with
        # images) so the frontend can interleave label and image.
        assert item.image_captions is not None and len(item.image_captions) == 1
        assert "[1] render.png" in item.image_captions[0]
        assert "after fix" in item.image_captions[0]
        # Caption text only — the base64 must never reach the payload.
        assert "base64" not in item.payload
        assert "data:image" not in item.payload
        assert "[1] render.png" in item.payload
        assert "after fix" in item.payload

    def test_attach_without_images_has_no_images_field(self, tmp_path: Path):
        # A text-only attach (e.g. a model that cannot receive media: the pack
        # still emits a caption message listing skipped files).
        msg = self._attach_message(
            tmp_path,
            blocks_override=[
                {
                    "type": "text",
                    "text": "[system] Files attached during this turn:\n- [1] x.png (image/png) — not delivered",
                }
            ],
        )
        items, _ = build_timeline_items([msg], [])
        assert len(items) == 1
        item = items[0]
        assert item.kind == "attach"
        assert item.images is None
        assert item.image_captions is None
        assert "not delivered" in item.payload

    def test_legacy_single_caption_block_has_no_image_captions(self, tmp_path: Path):
        # Pre-interleave attach messages stored ONE caption text block followed
        # by the image blocks; per-image pairing cannot be recovered there, so
        # image_captions stays None and the frontend falls back to the legacy
        # all-text-then-all-images layout instead of mispairing.
        msg = self._attach_message(
            tmp_path,
            blocks_override=[
                {"type": "text", "text": "[system] Files attached:\n- [1] a.png (image/png)"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,QUJDRA=="},
                },
            ],
        )
        items, _ = build_timeline_items([msg], [])
        assert len(items) == 1
        item = items[0]
        assert item.kind == "attach"
        assert item.images == ["data:image/png;base64,QUJDRA=="]
        assert item.image_captions is None

    def test_multiple_images_carry_aligned_image_captions(self, tmp_path: Path):
        # Two delivered images with interleaved blocks: image_captions must be
        # the two per-file caption lines in image order, and skipped entries
        # must not shift the alignment.
        msg = self._attach_message(
            tmp_path,
            blocks_override=[
                {
                    "type": "text",
                    "text": "[system] Files attached during this turn:",
                },
                {"type": "text", "text": '- [1] first.png (image/png, 1 B) — "one"'},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,RklyU1Q="},
                },
                {"type": "text", "text": "- [2] notes.txt (unknown) — not delivered"},
                {"type": "text", "text": '- [3] second.png (image/png, 2 B) — "two"'},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,U0VDT05E"},
                },
            ],
        )
        items, _ = build_timeline_items([msg], [])
        assert len(items) == 1
        item = items[0]
        assert item.images == ["data:image/png;base64,RklyU1Q=", "data:image/png;base64,U0VDT05E"]
        assert item.image_captions == [
            '- [1] first.png (image/png, 1 B) — "one"',
            '- [3] second.png (image/png, 2 B) — "two"',
        ]
        # The joined payload keeps every line (notice + all three entries).
        assert item.payload == (
            "[system] Files attached during this turn:\n"
            '- [1] first.png (image/png, 1 B) — "one"\n'
            "- [2] notes.txt (unknown) — not delivered\n"
            '- [3] second.png (image/png, 2 B) — "two"'
        )

    def test_non_image_media_blocks_never_leak_into_payload_or_images(self, tmp_path: Path):
        # pdf document blocks + media blocks are not thumbnailable; they must be
        # ignored for images AND their bytes must not leak into the payload.
        msg = self._attach_message(
            tmp_path,
            blocks_override=[
                {"type": "text", "text": "caption"},
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": "cGVuZGluZw==",
                    },
                },
                {"type": "media", "mime_type": "video/mp4", "data": b"video-bytes"},
            ],
        )
        items, _ = build_timeline_items([msg], [])
        assert len(items) == 1
        item = items[0]
        assert item.kind == "attach"
        assert item.images is None
        assert item.payload == "caption"
        assert "cGVuZGluZw==" not in item.payload
        assert "video-bytes" not in item.payload

    def test_attach_position_in_mixed_conversation(self, tmp_path: Path):
        # The attach message lands right after the exec-output ToolMessage in
        # state.messages (exec-node drain, user ruling 2026-08-26); item ids
        # must keep absolute msg_idx alignment.
        from langchain_core.messages import AIMessage

        from agent.messages import exec_output_message

        tool_call = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "execute_code",
                    "args": {"code": "x = 1"},
                    "id": "tc-1",
                    "type": "tool_call",
                }
            ],
        )
        output = exec_output_message(content="ok", tool_call_id="tc-1", exit_code=0)
        attach = self._attach_message(tmp_path)
        items, count = build_timeline_items([tool_call, output, attach], [])
        assert count == 3
        kinds = [it.kind for it in items]
        assert kinds == ["agent_code", "code_output", "attach"]
        assert items[2].item_id == "2.0"


class TestTimelineDispatch:
    """End-to-end dispatch chain tests: load real state.messages into PostgresSaver
    checkpoint, run the full GET /timeline endpoint, verify the returned items'
    order + kind + item_id.

    Difference from `TestAiMessageItems`: that directly feeds AIMessage to the helper,
    **bypassing the _load_langgraph_timeline_items dispatch chain** — the helper unit
    tests may all pass, but if the dispatch branch is miswired (e.g. AIMessage elif header
    accidentally deleted, embedded inside the lifecycle branch), the snapshot still doesn't
    return AIMessage content; the frontend renders as "two inbounds squeezed together +
    reasoning/chat/code floating at the end" in a scrambled order. This class prevents
    regression: the full messages list → endpoint output must be verified end-to-end.
    """

    @staticmethod
    def _put_checkpoint(agent_id: int, messages: list) -> None:
        """Directly use PostgresSaver.put to set a checkpoint with
        channel_values.messages = `messages`. Bypass the entire graph, so tests
        only care about dispatch behavior."""
        from langgraph.checkpoint.base import empty_checkpoint
        from langgraph.checkpoint.postgres import PostgresSaver

        from shared.config import settings

        ckpt = empty_checkpoint()
        ckpt["channel_values"] = {"messages": messages}
        # `__start__` is a LangGraph internal channel; messages is what we care about.
        # Give both a version so PostgresSaver.put serializes messages into the blobs table.
        ckpt["channel_versions"] = {"messages": "1", "__start__": "1"}

        with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
            saver.setup()
            saver.put(
                config={
                    "configurable": {
                        "thread_id": str(agent_id),
                        "checkpoint_ns": "",
                    }
                },
                checkpoint=ckpt,
                metadata={"source": "input", "step": 1, "parents": {}},
                new_versions={"messages": "1"},
            )

    def test_full_two_turn_conversation_renders_all_blocks(
        self, db_conn: psycopg.Connection, test_client: TestClient
    ) -> None:
        """Main regression test: two rounds of complete conversation (inbound → AIMessage → exec_output) × 2,
        each AIMessage contains three blocks: thinking + text + tool_use. The endpoint must return
        all items in msg_idx order; the three blocks of AIMessage all go into the timeline.

        Bug scene (8a3c520): AIMessage elif header lost, all three blocks silently dropped,
        snapshot only had [inbound_1, exec_output_1, inbound_2, exec_output_2] — the partial
        reasoning/chat/code accumulated by frontend streaming landed in clientExtras floating at the end.
        """
        from langchain_core.messages import AIMessage, ToolMessage

        from agent.messages import inbound_message
        from shared.db import insert_inbound_message

        tid = create_agent(db_conn)
        # Real inbound rows as ts anchor (the timeline endpoint uses inbound_messages
        # table to estimate the ts for the inbound HumanMessage)
        insert_inbound_message(db_conn, tid, "msg 1", source="user", kind="chat")
        insert_inbound_message(db_conn, tid, "msg 2", source="user", kind="chat")
        db_conn.commit()

        # Simulate the state.messages after the graph ran two rounds — msg_idx corresponds
        # one-to-one with enumeration position
        messages = [
            inbound_message(content="envelope:msg 1", source="user", inbound_id=1),  # 0
            AIMessage(  # 1
                content=[
                    {"type": "thinking", "thinking": "thinking 1", "index": 0},
                    {"type": "text", "text": "reply 1", "index": 1},
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "execute_code",
                        "input": {"code": "print('one')"},
                        "index": 2,
                    },
                ],
                tool_calls=[
                    {"name": "execute_code", "args": {"code": "print('one')"}, "id": "call_1"}
                ],
            ),
            ToolMessage(  # 2
                content="one\n",
                tool_call_id="call_1",
                additional_kwargs={"ava_msg_type": "exec_output"},
            ),
            inbound_message(content="envelope:msg 2", source="user", inbound_id=2),  # 3
            AIMessage(  # 4
                content=[
                    {"type": "thinking", "thinking": "thinking 2", "index": 0},
                    {"type": "text", "text": "reply 2", "index": 1},
                    {
                        "type": "tool_use",
                        "id": "call_2",
                        "name": "execute_code",
                        "input": {"code": "print('two')"},
                        "index": 2,
                    },
                ],
                tool_calls=[
                    {"name": "execute_code", "args": {"code": "print('two')"}, "id": "call_2"}
                ],
            ),
            ToolMessage(  # 5
                content="two\n",
                tool_call_id="call_2",
                additional_kwargs={"ava_msg_type": "exec_output"},
            ),
        ]
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        items = resp.json()["items"]

        # Complete order + item_id must strictly align with msg_idx.block_idx to match streaming SSE
        assert [(it["kind"], it["item_id"]) for it in items] == [
            ("inbound_chat", "0.0"),
            ("agent_reasoning", "1.0"),
            ("agent_chat", "1.1"),
            ("agent_code", "1.2"),
            ("code_output", "2.0"),
            ("inbound_chat", "3.0"),
            ("agent_reasoning", "4.0"),
            ("agent_chat", "4.1"),
            ("agent_code", "4.2"),
            ("code_output", "5.0"),
        ]
        assert [it["payload"] for it in items if it["kind"] == "agent_code"] == [
            "print('one')",
            "print('two')",
        ]

    def test_lifecycle_marker_does_not_swallow_following_aimessage(
        self, db_conn: psycopg.Connection, test_client: TestClient
    ) -> None:
        """Regression prevention (the specific refactor direction that caused the bug): the
        system_note branch must not mix in the AIMessage dispatch logic. If someone
        mistakenly embeds the _ai_message_items() call inside the system_note branch body,
        a system_note (HumanMessage) would trigger _ai_message_items's
        `assert isinstance(msg, AIMessage)` AssertionError, the entire dispatch chain
        would be silently swallowed by the outer except Exception — subsequent messages
        would all be lost.
        """
        from langchain_core.messages import AIMessage

        from agent.messages import NoteTag, inbound_message, system_note_message

        tid = create_agent(db_conn)
        from shared.db import insert_inbound_message

        insert_inbound_message(db_conn, tid, "before", source="user", kind="chat")
        db_conn.commit()

        messages = [
            inbound_message(content="before", source="user", inbound_id=1),  # 0
            system_note_message(  # 1 — system_note branch (lifecycle tag)
                content="[system] You have been restarted",
                tag=NoteTag.LIFECYCLE_RESTART,
            ),
            AIMessage(content="after restart, hello"),  # 2 — must render
        ]
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        items = resp.json()["items"]

        assert [(it["kind"], it["item_id"]) for it in items] == [
            ("inbound_chat", "0.0"),
            ("system_marker", "1.0"),
            ("agent_chat", "2.0"),
        ]
        # system_note's source field exposes the note tag (frontend chip uses it)
        lifecycle = items[1]
        assert lifecycle["source"] == "lifecycle_restart"
        # AIMessage renders completely
        assert items[2]["payload"] == "after restart, hello"

    def test_system_prompt_renders_at_index_zero_and_shifts_rest(
        self, db_conn: psycopg.Connection, test_client: TestClient
    ) -> None:
        """SystemMessage at state.messages[0] renders end-to-end as a
        system_prompt item at "0.0"; because it occupies index 0, the following
        inbound/AIMessage item_ids shift up by one (msg_idx is 1:1 with the
        position in state.messages). Pins the full dispatch path, not just the
        build_timeline_items unit."""
        from langchain_core.messages import AIMessage, SystemMessage

        from agent.messages import inbound_message
        from shared.db import insert_inbound_message

        tid = create_agent(db_conn)
        insert_inbound_message(db_conn, tid, "hi", source="user", kind="chat")
        db_conn.commit()

        messages = [
            SystemMessage(content="You are Ava."),  # 0
            inbound_message(content="envelope:hi", source="user", inbound_id=1),  # 1
            AIMessage(content="hello back"),  # 2
        ]
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [(it["kind"], it["item_id"]) for it in items] == [
            ("system_prompt", "0.0"),
            ("inbound_chat", "1.0"),
            ("agent_chat", "2.0"),
        ]
        assert items[0]["payload"] == "You are Ava."
        assert items[0]["created_at"] is None

    def test_aimessage_string_content_renders_as_chat(
        self, db_conn: psycopg.Connection, test_client: TestClient
    ) -> None:
        """legacy / no-tools coerce path: when AIMessage.content is a string, the
        whole thing goes into agent_chat (block_idx=0). Must pass through dispatch
        to reach this path."""
        from langchain_core.messages import AIMessage

        tid = create_agent(db_conn)
        messages = [AIMessage(content="plain string reply")]
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [(it["kind"], it["item_id"], it["payload"]) for it in items] == [
            ("agent_chat", "0.0", "plain string reply"),
        ]

    def test_items_ordered_by_item_id_not_created_at(
        self, db_conn: psycopg.Connection, test_client: TestClient
    ) -> None:
        """The endpoint orders items by item_id (msg_idx.block_idx) — the logical
        append order — not by created_at. Two AIMessages whose real ava_created_at
        runs backward vs their position must still render in position order; a
        created_at sort would flip them."""
        from langchain_core.messages import AIMessage

        tid = create_agent(db_conn)
        messages = [
            AIMessage(
                content="first", additional_kwargs={"ava_created_at": "2026-06-20T10:00:00+00:00"}
            ),
            AIMessage(
                content="second", additional_kwargs={"ava_created_at": "2026-06-19T08:00:00+00:00"}
            ),
        ]
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]
        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        assert [it["item_id"] for it in resp.json()["items"]] == ["0.0", "1.0"]


def test_item_sort_key_is_numeric_not_lexical() -> None:
    """item_id ordering is numeric (msg_idx, block_idx), so "10.0" follows "2.0"
    and "3.10" follows "3.2" — a lexical sort would get both backwards."""
    from gateway.routers.timeline import _item_sort_key

    assert _item_sort_key("2.0") < _item_sort_key("10.0")
    assert _item_sort_key("3.2") < _item_sort_key("3.10")

    def test_attach_message_renders_through_full_dispatch(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        tmp_path: Path,
    ) -> None:
        """End-to-end: an attach HumanMessage in the checkpoint must come back
        as a single kind=attach item with caption-only payload + image data
        URIs — not the old red system_marker with the base64 str()-d into the
        payload (Task #1668)."""
        from PIL import Image

        from agent.messages import attach_message
        from shared.lm.attach import AttachEntry, pack_attachments

        image = tmp_path / "render.png"
        Image.new("RGB", (2, 2)).save(image)
        pack = pack_attachments(
            "deepseek-v4-flash-vision-exp",
            [AttachEntry(path=str(image.resolve()), label="brand")],
        )
        assert pack is not None
        from datetime import UTC, datetime

        attach = attach_message(blocks=pack.blocks, text=pack.text, created_at=datetime.now(UTC))
        tid = create_agent(db_conn)
        self._put_checkpoint(tid, [attach])  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        body = resp.json()
        assert body["msg_count"] == 1
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["kind"] == "attach"
        assert item["source"] is None
        assert "[1] render.png" in item["payload"]
        assert "base64" not in item["payload"]
        assert "data:image/png;base64," in item["images"][0]


class TestTimelineFailLoud:
    """endpoint error handling boundaries: IO errors return empty list 200 (debug log),
    dispatch errors raise to 500 fail-loud.

    Historical lesson (8a3c520 → fixed in #50): during the refactor, a missing import +
    missing elif branch header caused NameError to be swallowed by the outer except Exception,
    the timeline silently truncated and users saw garbled order instead of a 500. This class
    prevents regression: any logic error in the dispatch layer must propagate as a 500.
    """

    @staticmethod
    def _put_minimal_aimessage(agent_id: int) -> None:
        """Load a single AIMessage so dispatch hits the _ai_message_items path.
        Tests use monkeypatch to replace the helper, triggering a RuntimeError
        to simulate a dispatch error."""
        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.base import empty_checkpoint
        from langgraph.checkpoint.postgres import PostgresSaver

        from shared.config import settings

        ckpt = empty_checkpoint()
        ckpt["channel_values"] = {"messages": [AIMessage(content="hello")]}
        ckpt["channel_versions"] = {"messages": "1", "__start__": "1"}
        with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
            saver.setup()
            saver.put(
                config={
                    "configurable": {
                        "thread_id": str(agent_id),
                        "checkpoint_ns": "",
                    }
                },
                checkpoint=ckpt,
                metadata={"source": "input", "step": 1, "parents": {}},
                new_versions={"messages": "1"},
            )

    def test_dispatch_error_propagates_as_500(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Any logic error in the dispatch layer (NameError / KeyError / custom helper raise)
        must propagate as endpoint 500 — must not be swallowed into 200 + empty list.

        Bug reproduce: replace _ai_message_items with a raise RuntimeError, feed one
        AIMessage into state. Old code (try wrapped the entire loop): endpoint 200 +
        empty list + debug log swallowed. New code (try only wraps saver IO): 500.

        Note raise_app_exceptions=False: by default TestClient re-raises endpoint exceptions
        to the test assertion layer (convenient for debugging), but this test needs to see
        the HTTP-layer 500 status, so disable re-raise to let FastAPI run the default error
        middleware and return 500.
        """
        from gateway.app import app
        from shared import timeline as shared_timeline

        tid = create_agent(db_conn)
        self._put_minimal_aimessage(tid)

        def boom(*_args, **_kwargs):
            raise RuntimeError(
                "simulated dispatch failure (e.g. NameError when refactor forgot import)"
            )

        monkeypatch.setattr(shared_timeline, "_ai_message_items", boom)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 500, (
            f"dispatch error must fail-loud as 500, got {resp.status_code}: {resp.text[:200]}"
        )

    def test_io_error_returns_empty_list_with_200(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """IO layer errors (DB connection drop / deserialization failure) still follow the
        current contract returning empty list 200 — timeline is a cold-load view, when
        checkpoint read fails, don't block the UI (frontend gets no timeline but can continue
        rendering other pages).

        Path: load_checkpoint_messages raises IO failures as CheckpointReadError,
        get_timeline catches it → warning log + messages=[]. Prevents the fail-loud
        refactor from going too far and turning read failures into 500 affecting UX
        (contrast with the /messages data endpoint 503: that doesn't tolerate, this one does).
        """

        tid = create_agent(db_conn)
        # Trigger IO error: replace PostgresSaver.from_conn_string with a context manager
        # that raises OSError (simulating DB network disruption)
        from contextlib import contextmanager

        @contextmanager
        def fake_saver(_url):
            raise OSError("simulated DB connection lost")
            yield  # type: ignore[unreachable]

        # load_checkpoint_messages imports PostgresSaver inside the function
        # from langgraph.checkpoint.postgres, so patch the source module.
        import langgraph.checkpoint.postgres as ckpt_mod

        class FakeSaver:
            from_conn_string = staticmethod(fake_saver)  # pyright: ignore[reportUnknownArgumentType]

        monkeypatch.setattr(ckpt_mod, "PostgresSaver", FakeSaver)
        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        assert resp.json() == {"items": [], "msg_count": 0, "has_more": False}
        # Side effect: get_timeline's _log should have had a warning trace (operators can grep)
        # Do not assert log content — avoid coupling with logger implementation details


def test_timeline_skips_inbound_without_langgraph_state(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """Data in the inbound_messages table does not directly enter the timeline — it only
    appears after the claim node envelope-wraps the HumanMessage into the LangGraph state.
    Here we only INSERT an inbound without running the graph; the timeline should not see
    this inbound."""
    tid = create_agent(db_conn)
    insert_inbound_message(db_conn, tid, "a user message but graph didn't run", source="user")

    resp = test_client.get(f"/api/agents/{tid}/timeline")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [it for it in items if it["kind"] == "inbound_chat"] == []


def test_timeline_anchor_filter_only_includes_chat_inbounds(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """The anchor sequence only takes inbounds with kind='chat'. Lifecycle inbounds
    (resurrect / restart_completed / terminate / restart) even if they exist in the
    table do not enter the anchor — the claim side dispatches them as
    ava_msg_type='lifecycle' HumanMessage, decoupled from the chat anchor sequence,
    preventing misaligned chat timestamp advancement.

    Regression prevention: if someone "simplifies" to kind in ('chat', 'resurrect', ...)
    and includes lifecycle as anchors, timeline rendering would consume chat anchor slots
    with lifecycle inbounds, and chat HumanMessages would get wrong timestamps. This test
    inserts a mixed sequence and verifies the endpoint still returns 200 and lifecycle
    inbounds do not pollute the chat anchor.
    """
    from shared.db import list_inbound_messages
    from shared.timeline import build_timeline_items

    tid = create_agent(db_conn)
    # Mixed chat / lifecycle kinds, in INSERT order
    insert_inbound_message(db_conn, tid, "chat 1", source="user", kind="chat")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, '', 'resurrect', 'user')",
            (tid,),
        )
    insert_inbound_message(db_conn, tid, "chat 2", source="user", kind="chat")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, '', 'terminate', 'user')",
            (tid,),
        )
    insert_inbound_message(db_conn, tid, "chat 3", source="user", kind="chat")
    db_conn.commit()

    # Directly check the internal helper to see the anchor list (bypass full endpoint,
    # no LangGraph state needed)
    inbound_anchors = [
        row for row in list_inbound_messages(db_conn, tid, 500) if row.kind == "chat"
    ]
    assert len(inbound_anchors) == 3
    assert [r.content for r in inbound_anchors] == ["chat 1", "chat 2", "chat 3"]

    # Overall endpoint still returns 200 normally (no LangGraph state, items are 0 but
    # should not raise)
    resp = test_client.get(f"/api/agents/{tid}/timeline")
    assert resp.status_code == 200
    # No messages → render empty list, msg_count=0 — render does not raise
    assert build_timeline_items([], inbound_anchors) == ([], 0)


def test_item_created_at_prefers_real_ava_created_at_over_synthetic() -> None:
    """A message carrying a real `ava_created_at` surfaces it verbatim — the
    timeline shows the message's own wall-clock time, not the synthetic
    anchor+microsecond offset (which renders as 1970 when no chat preceded it)."""
    from langchain_core.messages import ToolMessage

    from shared.timeline import build_timeline_items

    msg = ToolMessage(
        content="out",
        tool_call_id="t1",
        additional_kwargs={
            "ava_msg_type": "exec_output",
            "ava_created_at": "2026-06-19T12:00:00+00:00",
        },
    )
    items, _ = build_timeline_items([msg], [])
    assert items[0].created_at == "2026-06-19T12:00:00+00:00"


def test_inbound_with_real_ts_still_advances_anchor_for_legacy_siblings() -> None:
    """A new inbound carries its own `ava_created_at` AND still consumes the chat
    anchor, so a following legacy item (no `ava_created_at`) falls back to that
    inbound's anchor time — not epoch 0. Pins the mixed-history case: a long-lived
    agent restarted onto new code has legacy messages (anchor fallback) and new
    messages (real ts) interleaved."""
    from datetime import UTC, datetime

    from langchain_core.messages import HumanMessage, ToolMessage

    from shared.db import InboundRow
    from shared.timeline import build_timeline_items

    # In production, the inbound's ava_created_at IS the anchor row's created_at
    # (same DB row); here they are set apart only so the two assertions can tell
    # "used real ts" (15:30) from "used the anchor" (12:00).
    anchor_dt = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)
    inbound = HumanMessage(
        content="hi",
        additional_kwargs={
            "ava_msg_type": "inbound",
            "ava_source": "ui:web",
            "ava_inbound_id": 7,
            "ava_created_at": "2026-06-19T15:30:00+00:00",
        },
    )
    legacy_exec = ToolMessage(
        content="out", tool_call_id="t1", additional_kwargs={"ava_msg_type": "exec_output"}
    )
    anchors = [InboundRow(7, "hi", "chat", "ui:web", "claimed", anchor_dt)]
    items, _ = build_timeline_items([inbound, legacy_exec], anchors)
    assert items[0].created_at == "2026-06-19T15:30:00+00:00"  # inbound shows its own real ts
    sibling_ts = items[1].created_at
    assert sibling_ts is not None
    assert sibling_ts.startswith("2026-06-19T12:00:00")  # legacy sibling -> anchor
    assert not sibling_ts.startswith("1970")


def test_compacted_inbound_uses_its_embedded_id_instead_of_oldest_anchor() -> None:
    """A compacted checkpoint can start long after the agent's first DB inbound.

    The message's ``ava_inbound_id`` is the durable correlation key.  Timeline
    rendering must use it to select the matching row rather than pairing the
    surviving message with the oldest historical anchor by list position.
    """
    from datetime import UTC, datetime

    from langchain_core.messages import HumanMessage, ToolMessage

    from shared.db import InboundRow
    from shared.timeline import build_timeline_items

    stale_anchor = InboundRow(
        4516,
        "old compacted-away message",
        "chat",
        "ui:web",
        "done",
        datetime(2026, 6, 18, 9, 0, tzinfo=UTC),
    )
    matching_anchor = InboundRow(
        70598,
        "current message",
        "chat",
        "user",
        "claimed",
        datetime(2026, 8, 22, 17, 47, tzinfo=UTC),
    )
    inbound = HumanMessage(
        content="current message",
        additional_kwargs={
            "ava_msg_type": "inbound",
            "ava_source": "user",
            "ava_inbound_id": 70598,
            "ava_created_at": "2026-08-22T17:47:00+00:00",
        },
    )
    legacy_sibling = ToolMessage(
        content="output",
        tool_call_id="t1",
        additional_kwargs={"ava_msg_type": "exec_output"},
    )

    items, _ = build_timeline_items([inbound, legacy_sibling], [stale_anchor, matching_anchor])

    assert items[0].inbound_id == 70598
    assert items[1].created_at is not None
    assert items[1].created_at.startswith("2026-08-22T17:47:00")


@pytest.mark.parametrize("malformed_id", ["70598", True, False, 0, -1, None])
def test_inbound_rejects_malformed_embedded_id(malformed_id: object) -> None:
    """A present correlation key is contractual, never a legacy fallback hint."""
    from langchain_core.messages import HumanMessage

    from shared.timeline import build_timeline_items

    inbound = HumanMessage(
        content="message",
        additional_kwargs={
            "ava_msg_type": "inbound",
            "ava_source": "user",
            "ava_inbound_id": malformed_id,
            "ava_created_at": "2026-08-22T17:47:00+00:00",
        },
    )

    with pytest.raises(ValueError, match="ava_inbound_id"):
        build_timeline_items([inbound], [])


def test_missing_embedded_anchor_does_not_consume_legacy_fallback() -> None:
    """A missing exact match leaves later positional legacy anchors intact."""
    from datetime import UTC, datetime

    from langchain_core.messages import HumanMessage, ToolMessage

    from shared.db import InboundRow
    from shared.timeline import build_timeline_items

    missing_modern = HumanMessage(
        content="row no longer present",
        additional_kwargs={
            "ava_msg_type": "inbound",
            "ava_source": "user",
            "ava_inbound_id": 99,
            "ava_created_at": "2026-08-22T17:47:00+00:00",
        },
    )
    legacy_inbound = HumanMessage(
        content="legacy",
        additional_kwargs={"ava_msg_type": "inbound", "ava_source": "ui:web"},
    )
    legacy_output = ToolMessage(
        content="output",
        tool_call_id="t1",
        additional_kwargs={"ava_msg_type": "exec_output"},
    )
    legacy_anchor = InboundRow(
        10,
        "legacy",
        "chat",
        "ui:web",
        "done",
        datetime(2026, 8, 22, 18, 0, tzinfo=UTC),
    )

    items, _ = build_timeline_items(
        [missing_modern, legacy_inbound, legacy_output], [legacy_anchor]
    )

    assert [item.inbound_id for item in items[:2]] == [99, 10]
    assert items[2].created_at is not None
    assert items[2].created_at.startswith("2026-08-22T18:00:00")


def test_out_of_order_and_duplicate_embedded_ids_preserve_anchor_cursor() -> None:
    """Exact lookups never rewind or exhaust the legacy positional cursor."""
    from datetime import UTC, datetime

    from langchain_core.messages import HumanMessage

    from shared.db import InboundRow
    from shared.timeline import build_timeline_items

    def modern(inbound_id: int) -> HumanMessage:
        return HumanMessage(
            content=str(inbound_id),
            additional_kwargs={
                "ava_msg_type": "inbound",
                "ava_source": "user",
                "ava_inbound_id": inbound_id,
                "ava_created_at": f"2026-08-22T18:{inbound_id}:00+00:00",
            },
        )

    legacy = HumanMessage(
        content="legacy",
        additional_kwargs={"ava_msg_type": "inbound", "ava_source": "ui:web"},
    )
    anchors = [
        InboundRow(
            inbound_id,
            str(inbound_id),
            "chat",
            "user",
            "done",
            datetime(2026, 8, 22, 18, minute, tzinfo=UTC),
        )
        for inbound_id, minute in [(10, 10), (20, 20), (30, 30)]
    ]

    items, _ = build_timeline_items([modern(20), modern(10), modern(20), legacy], anchors)

    assert [item.inbound_id for item in items] == [20, 10, 20, 30]


def test_aimessage_blocks_share_one_real_ava_created_at() -> None:
    """All blocks of one AIMessage carry the message's single real ts — the
    timeline no longer fans its reasoning/text/code items out across synthetic
    per-block microsecond offsets."""
    from langchain_core.messages import AIMessage

    from shared.timeline import build_timeline_items

    msg = AIMessage(
        content=[
            {"type": "thinking", "thinking": "hmm", "index": 0},
            {"type": "text", "text": "done", "index": 1},
        ],
        additional_kwargs={"ava_created_at": "2026-06-19T12:00:00+00:00"},
    )
    items, _ = build_timeline_items([msg], [])
    assert [it.created_at for it in items] == [
        "2026-06-19T12:00:00+00:00",
        "2026-06-19T12:00:00+00:00",
    ]


def _items(*ids: str) -> list[TimelineItem]:
    return [TimelineItem(item_id=i, kind="agent_chat", payload=i) for i in ids]


class TestTimelineWindowing:
    """tail_window / _window_before pure-function contract — the slicing
    behind GET /timeline pagination + the agent-published snapshot trim
    (no DB)."""

    def test_tail_window_shorter_than_limit_returns_all_no_more(self) -> None:
        window, has_more = tail_window(_items("1.0", "2.0"), 5)
        assert [i.item_id for i in window] == ["1.0", "2.0"]
        assert has_more is False

    def test_tail_window_trims_to_newest_and_flags_more(self) -> None:
        window, has_more = tail_window(_items("1.0", "2.0", "3.0", "4.0"), 2)
        assert [i.item_id for i in window] == ["3.0", "4.0"]
        assert has_more is True

    def test_window_before_returns_items_immediately_older(self) -> None:
        window, has_more = _window_before(_items("1.0", "2.0", "3.0", "4.0", "5.0"), "4.0", 2)
        assert [i.item_id for i in window] == ["2.0", "3.0"]
        assert has_more is True  # 1.0 still older than the window

    def test_window_before_reaching_start_has_no_more(self) -> None:
        window, has_more = _window_before(_items("1.0", "2.0", "3.0"), "3.0", 5)
        assert [i.item_id for i in window] == ["1.0", "2.0"]
        assert has_more is False

    def test_window_before_unknown_cursor_returns_empty(self) -> None:
        window, has_more = _window_before(_items("1.0", "2.0"), "9.9", 5)
        assert window == []
        assert has_more is False


class TestMultimodalInbound:
    """A multimodal inbound HumanMessage (list content: a text block + native
    base64 image blocks) renders as an inbound_chat item whose payload is the
    text part and whose `images` are the reference urls — the base64 in the
    message content is never str()-d into the payload."""

    @staticmethod
    def _msg() -> HumanMessage:
        return HumanMessage(
            content=[
                {"type": "text", "text": "User:\n\nwhat is this?"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
                },
            ],
            additional_kwargs={
                "ava_msg_type": "inbound",
                "ava_source": "user",
                "ava_inbound_id": 1,
                "ava_image_urls": ["/api/agents/7/uploads/shot.png"],
            },
        )

    def test_renders_text_and_images_not_base64(self) -> None:
        from shared.timeline import build_timeline_items

        items, _ = build_timeline_items([self._msg()], [])
        (item,) = items
        assert item.kind == "inbound_chat"
        assert item.payload == "User:\n\nwhat is this?"
        assert item.images == ["/api/agents/7/uploads/shot.png"]
        # The base64 payload must never leak into the rendered text.
        assert "QUJD" not in item.payload

    def test_image_only_payload_is_placeholder_text(self) -> None:
        from shared.timeline import build_timeline_items

        msg = HumanMessage(
            content=[
                {"type": "text", "text": "User:\n\n[image]"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
                },
            ],
            additional_kwargs={
                "ava_msg_type": "inbound",
                "ava_source": "user",
                "ava_inbound_id": 2,
                "ava_image_urls": ["/api/agents/7/uploads/a.png"],
            },
        )
        items, _ = build_timeline_items([msg], [])
        assert items[0].images == ["/api/agents/7/uploads/a.png"]
        assert "QUJD" not in items[0].payload


class TestSystemPromptInColdLoad:
    """#570: the expandable prompt card's data source.

    `tail_window` returns only the newest `limit` items, so any conversation
    with more than 50 rendered items loses the oldest item — the ~128KB
    system-prompt item 0.0 — and the prompt card would render empty on first
    open and never recover (SSE snapshots window too, and the frontend can't
    cold-load older windows without this endpoint). The endpoint must always
    serve 0.0 on the default window.
    """

    @staticmethod
    def _put_checkpoint(agent_id: int, messages: list) -> None:
        from langgraph.checkpoint.base import empty_checkpoint
        from langgraph.checkpoint.postgres import PostgresSaver

        from shared.config import settings

        ckpt = empty_checkpoint()
        ckpt["channel_values"] = {"messages": messages}
        ckpt["channel_versions"] = {"messages": "1", "__start__": "1"}
        with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
            saver.setup()
            saver.put(
                config={
                    "configurable": {
                        "thread_id": str(agent_id),
                        "checkpoint_ns": "",
                    }
                },
                checkpoint=ckpt,
                metadata={"source": "input", "step": 1, "parents": {}},
                new_versions={"messages": "1"},
            )

    def test_long_conversation_keeps_system_prompt(
        self, db_conn: psycopg.Connection, test_client: TestClient
    ) -> None:
        """>50 rendered items: 0.0 must still head the default window."""
        from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        messages: list[BaseMessage] = [SystemMessage(content="You are Ava.")]
        for i in range(60):
            messages.append(HumanMessage(content=f"user msg {i}"))
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        # 60 humans + 1 system prompt = 61 items; tail window = 50 + re-attached 0.0
        assert data["has_more"] is True
        assert data["items"][0]["item_id"] == "0.0"
        assert data["items"][0]["kind"] == "system_prompt"
        assert data["items"][0]["payload"] == "You are Ava."
        assert len(data["items"]) == 51
        # The rest is the newest window in order (0.0 is prepended, not replacing)
        assert data["items"][1]["item_id"] == "11.0"

    def test_short_conversation_does_not_duplicate(
        self, db_conn: psycopg.Connection, test_client: TestClient
    ) -> None:
        """<=50 items: the window already contains 0.0 — no duplicate entry."""
        from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        messages: list[BaseMessage] = [SystemMessage(content="You are Ava.")]
        for i in range(10):
            messages.append(HumanMessage(content=f"user msg {i}"))
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        ids = [it["item_id"] for it in data["items"]]
        assert ids[0] == "0.0"
        assert ids.count("0.0") == 1
        assert data["has_more"] is False

    def test_long_conversation_rehangs_compact_summary(
        self, db_conn: psycopg.Connection, test_client: TestClient
    ) -> None:
        """Compact summaries are standing context: in a long conversation the
        earliest inbound_compact_summary falls off the tail window and the
        re-attached 0.0 makes older paging unable to reach it — so GET must
        re-attach every compact_summary right after the prompt (user report
        2026-08-06). Not counted against `limit`; has_more recomputed."""
        from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        messages: list[BaseMessage] = [SystemMessage(content="You are Ava.")]
        messages.append(
            HumanMessage(
                content="Conversation summary line",
                additional_kwargs={
                    "ava_msg_type": "compact_summary",
                    "ava_created_at": "2026-08-06T00:00:00+00:00",
                },
            )
        )
        for i in range(60):
            messages.append(HumanMessage(content=f"user msg {i}"))
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        # 0.0 prompt, then the re-attached compact summary, then the newest window
        assert data["items"][0]["item_id"] == "0.0"
        assert data["items"][0]["kind"] == "system_prompt"
        assert data["items"][1]["item_id"] == "1.0"
        assert data["items"][1]["kind"] == "inbound_compact_summary"
        assert "Conversation summary line" in data["items"][1]["payload"]
        assert data["items"][2]["item_id"] == "12.0"
        assert len(data["items"]) == 52  # 50 + prompt + compact summary
        assert data["has_more"] is True

    def test_short_conversation_compact_summary_not_duplicated(
        self, db_conn: psycopg.Connection, test_client: TestClient
    ) -> None:
        """<=50 items: the window already contains the compact summary —
        no duplicate entry."""
        from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        messages: list[BaseMessage] = [SystemMessage(content="You are Ava.")]
        messages.append(
            HumanMessage(
                content="Short summary",
                additional_kwargs={
                    "ava_msg_type": "compact_summary",
                    "ava_created_at": "2026-08-06T00:00:00+00:00",
                },
            )
        )
        for i in range(10):
            messages.append(HumanMessage(content=f"user msg {i}"))
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        items = resp.json()["items"]
        compact = [it for it in items if it["kind"] == "inbound_compact_summary"]
        assert len(compact) == 1
        assert items[0]["item_id"] == "0.0"
        assert compact[0]["item_id"] == "1.0"

    def test_before_paging_does_not_carry_system_prompt(
        self, db_conn: psycopg.Connection, test_client: TestClient
    ) -> None:
        """Scroll-up paging (`before`) is history-only — 0.0 is not re-sent."""
        from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        messages: list[BaseMessage] = [SystemMessage(content="You are Ava.")]
        for i in range(60):
            messages.append(HumanMessage(content=f"user msg {i}"))
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        oldest = resp.json()["items"][-1]["item_id"]
        page = test_client.get(f"/api/agents/{tid}/timeline", params={"before": oldest})
        assert page.status_code == 200
        pdata = page.json()
        assert all(it["item_id"] != "0.0" for it in pdata["items"])


class TestBuildTimelineItemsStartOffset:
    """`build_timeline_items(..., start=N)` — the incremental-snapshot render
    path. Item ids keep their ABSOLUTE msg_idx; msg_count stays the FULL
    history length; anchors are NOT consumed when start > 0."""

    def test_start_offset_keeps_absolute_item_ids_and_full_msg_count(self):
        from langchain_core.messages import AIMessage, SystemMessage

        from shared.timeline import build_timeline_items

        messages = [
            SystemMessage(content="prompt"),
            AIMessage(content="first"),
            AIMessage(content="second"),
        ]
        items, msg_count = build_timeline_items(messages, [], start=2)
        assert msg_count == 3  # full length, never the window length
        assert [it.item_id for it in items] == ["2.0"]
        assert items[0].payload == "second"

    def test_start_zero_matches_no_offset(self):
        from langchain_core.messages import AIMessage, SystemMessage

        from shared.timeline import build_timeline_items

        messages = [SystemMessage(content="prompt"), AIMessage(content="first")]
        full, full_count = build_timeline_items(messages, [])
        sliced, sliced_count = build_timeline_items(messages, [], start=0)
        assert full_count == sliced_count
        assert [it.item_id for it in full] == [it.item_id for it in sliced]
        assert [it.payload for it in full] == [it.payload for it in sliced]

    def test_start_offset_does_not_consume_anchors(self):
        # An inbound inside the incremental window must NOT consume the first
        # historical anchor (it would misalign ts / inbound_id). Modern
        # messages carry ava_created_at, so the anchor list is irrelevant on
        # the incremental path — pass [] and the item still renders.
        from langchain_core.messages import HumanMessage, SystemMessage

        from shared.timeline import build_timeline_items

        msg = HumanMessage(
            content="hi",
            additional_kwargs={
                "ava_msg_type": "inbound",
                "ava_created_at": "2026-01-01T00:00:00+00:00",
            },
        )
        items, msg_count = build_timeline_items([SystemMessage(content="p"), msg], [], start=1)
        assert msg_count == 2
        assert items[0].item_id == "1.0"
        assert items[0].created_at == "2026-01-01T00:00:00+00:00"
        assert items[0].inbound_id is None

    def test_start_offset_keeps_modern_embedded_inbound_id_without_anchors(self):
        from langchain_core.messages import HumanMessage, SystemMessage

        from shared.timeline import build_timeline_items

        msg = HumanMessage(
            content="hi",
            additional_kwargs={
                "ava_msg_type": "inbound",
                "ava_source": "user",
                "ava_inbound_id": 70598,
                "ava_created_at": "2026-01-01T00:00:00+00:00",
            },
        )

        items, msg_count = build_timeline_items([SystemMessage(content="p"), msg], [], start=1)

        assert msg_count == 2
        assert items[0].item_id == "1.0"
        assert items[0].inbound_id == 70598

    def test_segment_prefix_keeps_local_message_and_block_positions(self):
        from langchain_core.messages import AIMessage, HumanMessage

        from shared.timeline import build_timeline_items

        items, msg_count = build_timeline_items(
            [
                HumanMessage(content="older inbound"),
                AIMessage(
                    content=[
                        {"type": "thinking", "thinking": "older reasoning", "index": 0},
                        {"type": "text", "text": "older answer", "index": 1},
                    ]
                ),
            ],
            [],
            segment_prefix="s2.1f0b9b12-0000-6000-8000-000000000000",
        )

        assert msg_count == 2
        assert [item.item_id for item in items] == [
            "s2.1f0b9b12-0000-6000-8000-000000000000.0.0",
            "s2.1f0b9b12-0000-6000-8000-000000000000.1.0",
            "s2.1f0b9b12-0000-6000-8000-000000000000.1.1",
        ]


class TestTimelineCompactHistory:
    @staticmethod
    def _summary(content: str) -> HumanMessage:
        return HumanMessage(
            content=content,
            additional_kwargs={
                "ava_msg_type": "compact_summary",
                "ava_created_at": "2026-08-25T00:00:00+00:00",
            },
        )

    @classmethod
    def _segment(cls, name: str, item_count: int) -> list[BaseMessage]:
        from langchain_core.messages import AIMessage, SystemMessage

        return [
            SystemMessage(content="system"),
            cls._summary(f"{name} summary"),
            *(AIMessage(content=f"{name} item {i}") for i in range(item_count)),
        ]

    @classmethod
    def _current(cls, *messages: BaseMessage) -> list[BaseMessage]:
        from langchain_core.messages import SystemMessage

        return [SystemMessage(content="system"), cls._summary("current summary"), *messages]

    @staticmethod
    def _put_checkpoint(
        agent_id: int,
        messages: list[BaseMessage],
        *,
        version: str,
        boundary: bool = False,
    ) -> str:
        from typing import cast

        from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
        from langgraph.checkpoint.postgres import PostgresSaver

        from shared.config import settings

        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"messages": messages}
        checkpoint["channel_versions"] = {"messages": version, "__start__": "1"}
        metadata: dict[str, object] = {"source": "input", "step": int(version), "parents": {}}
        if boundary:
            metadata["compact_boundary"] = True
        with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
            saver.setup()
            saved = saver.put(
                config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
                checkpoint=checkpoint,
                metadata=cast(CheckpointMetadata, metadata),
                new_versions={"messages": version},
            )
        return str((saved.get("configurable") or {})["checkpoint_id"])

    def test_initial_short_window_reports_history_only_when_enabled(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage

        from shared.config import settings

        tid = create_agent(db_conn)
        self._put_checkpoint(tid, self._segment("history", 2), version="1", boundary=True)
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="2",
        )

        monkeypatch.setattr(settings.gateway, "timeline_compact_history", 0)
        disabled = test_client.get(f"/api/agents/{tid}/timeline", params={"limit": 50}).json()
        assert disabled["has_more"] is False

        monkeypatch.setattr(settings.gateway, "timeline_compact_history", 1)
        enabled = test_client.get(f"/api/agents/{tid}/timeline", params={"limit": 50}).json()
        assert enabled["has_more"] is True
        assert all(not item["item_id"].startswith("s") for item in enabled["items"])

    def test_pages_within_segment_then_reaches_its_summary(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage

        from shared.config import settings

        tid = create_agent(db_conn)
        boundary_id = self._put_checkpoint(
            tid, self._segment("history", 5), version="1", boundary=True
        )
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="2",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", -1)

        crossed = test_client.get(
            f"/api/agents/{tid}/timeline", params={"before": "2.0", "limit": 2}
        ).json()
        assert [item["item_id"] for item in crossed["items"]] == [
            "0.0",
            "1.0",
            f"s1.{boundary_id}.4.0",
            f"s1.{boundary_id}.5.0",
        ]
        assert crossed["has_more"] is True

        middle = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{boundary_id}.4.0", "limit": 2},
        ).json()
        assert [item["item_id"] for item in middle["items"]] == [
            f"s1.{boundary_id}.2.0",
            f"s1.{boundary_id}.3.0",
        ]
        assert middle["has_more"] is True

        head = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{boundary_id}.2.0", "limit": 2},
        ).json()
        assert [item["item_id"] for item in head["items"]] == [
            f"s1.{boundary_id}.0.0",
            f"s1.{boundary_id}.1.0",
        ]
        assert head["items"][0]["kind"] == "inbound_compact_summary"
        assert head["has_more"] is False

    def test_exact_tail_boundary_delivers_summary_while_crossing_segments(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage

        from shared.config import settings

        tid = create_agent(db_conn)
        older_id = self._put_checkpoint(
            tid,
            self._segment("older", 1),
            version="1",
            boundary=True,
        )
        newer_id = self._put_checkpoint(
            tid,
            self._segment("newer", 50),
            version="2",
            boundary=True,
        )
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="3",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", -1)

        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{newer_id}.1.0", "limit": 50},
        ).json()

        assert [item["item_id"] for item in page["items"]] == [
            f"s1.{newer_id}.0.0",
            f"s2.{older_id}.0.0",
            f"s2.{older_id}.1.0",
        ]
        assert page["items"][0]["kind"] == "inbound_compact_summary"
        assert page["has_more"] is False

    def test_depth_limit_still_delivers_oldest_allowed_segment_summary(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage

        from shared.config import settings

        tid = create_agent(db_conn)
        self._put_checkpoint(tid, self._segment("blocked", 1), version="1", boundary=True)
        allowed_id = self._put_checkpoint(
            tid,
            self._segment("allowed", 50),
            version="2",
            boundary=True,
        )
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="3",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", 1)

        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{allowed_id}.1.0", "limit": 50},
        ).json()

        assert [item["item_id"] for item in page["items"]] == [f"s1.{allowed_id}.0.0"]
        assert page["items"][0]["kind"] == "inbound_compact_summary"
        assert page["has_more"] is False

    @pytest.mark.parametrize("depth", [-1, 1])
    def test_summary_only_segment_is_bounded_continuation_or_depth_terminal(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        depth: int,
    ) -> None:
        from langchain_core.messages import AIMessage

        from shared.config import settings

        tid = create_agent(db_conn)
        older_id = self._put_checkpoint(
            tid,
            self._segment("older", 1),
            version="1",
            boundary=True,
        )
        summary_only_id = self._put_checkpoint(
            tid,
            self._segment("summary only", 0),
            version="2",
            boundary=True,
        )
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="3",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", depth)

        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": "2.0", "limit": 50},
        ).json()

        assert [item["item_id"] for item in page["items"]] == [
            "0.0",
            "1.0",
            f"s1.{summary_only_id}.0.0",
        ]
        assert page["has_more"] is (depth == -1)
        if depth == -1:
            continued = test_client.get(
                f"/api/agents/{tid}/timeline",
                params={"before": f"s1.{summary_only_id}.0.0", "limit": 50},
            ).json()
            assert [item["item_id"] for item in continued["items"]] == [
                f"s2.{older_id}.0.0",
                f"s2.{older_id}.1.0",
            ]
            assert continued["has_more"] is False

    def test_positive_depth_bounds_boundary_index_read(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage

        import gateway.routers.timeline as timeline_router
        from shared.config import settings

        tid = create_agent(db_conn)
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="1",
        )
        requested_limits: list[int | None] = []

        def boundary_ids(_agent_id: int, *, limit: int | None = None) -> list[str]:
            requested_limits.append(limit)
            return []

        monkeypatch.setattr(timeline_router, "list_compact_boundary_checkpoint_ids", boundary_ids)
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", 3)
        response = test_client.get(f"/api/agents/{tid}/timeline")

        assert response.status_code == 200
        assert requested_limits == [4]

    def test_depth_and_checkpoint_id_control_cross_segment_access(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage

        from shared.config import settings

        tid = create_agent(db_conn)
        older_id = self._put_checkpoint(tid, self._segment("older", 2), version="1", boundary=True)
        newer_id = self._put_checkpoint(tid, self._segment("newer", 2), version="2", boundary=True)
        current_id = self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="3",
        )

        monkeypatch.setattr(settings.gateway, "timeline_compact_history", 1)
        blocked = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s2.{older_id}.1.0", "limit": 50},
        ).json()
        assert blocked == {"items": [], "msg_count": 3, "has_more": False}

        non_boundary = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{current_id}.1.0", "limit": 50},
        ).json()
        assert non_boundary == {"items": [], "msg_count": 3, "has_more": False}

        invalid_rank = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s0.{newer_id}.1.0", "limit": 50},
        ).json()
        assert invalid_rank == {"items": [], "msg_count": 3, "has_more": False}

        monkeypatch.setattr(settings.gateway, "timeline_compact_history", -1)
        stale_rank = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s99.{newer_id}.2.0", "limit": 1},
        ).json()
        assert [item["item_id"] for item in stale_rank["items"]] == [f"s1.{newer_id}.1.0"]

        head = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s99.{newer_id}.2.0", "limit": 2},
        ).json()
        assert [item["item_id"] for item in head["items"]] == [
            f"s1.{newer_id}.0.0",
            f"s1.{newer_id}.1.0",
        ]
        assert head["has_more"] is True

        crossed = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{newer_id}.1.0", "limit": 50},
        ).json()
        assert [item["item_id"] for item in crossed["items"]] == [
            f"s1.{newer_id}.0.0",
            f"s2.{older_id}.0.0",
            f"s2.{older_id}.1.0",
            f"s2.{older_id}.2.0",
        ]
        assert crossed["has_more"] is False

    def test_historical_page_does_not_deserialize_the_current_segment(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage

        import gateway.routers.timeline as timeline_router
        from shared.config import settings

        tid = create_agent(db_conn)
        boundary_id = self._put_checkpoint(
            tid, self._segment("history", 2), version="1", boundary=True
        )
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="2",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", 1)

        def fail_current_read(_agent_id: int) -> list[BaseMessage]:
            raise AssertionError("historical paging must not deserialize the live segment")

        monkeypatch.setattr(timeline_router, "load_checkpoint_messages", fail_current_read)

        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{boundary_id}.2.0", "limit": 1},
        )

        assert page.status_code == 200
        assert page.json()["msg_count"] == 3
        assert [item["item_id"] for item in page.json()["items"]] == [f"s1.{boundary_id}.1.0"]

    @pytest.mark.parametrize(
        "before",
        [
            f"{'1' * 5000}.0",
            f"s1.boundary.1.{'0' * 5000}",
        ],
    )
    def test_oversized_numeric_cursor_is_terminal_not_500(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        before: str,
    ) -> None:
        from langchain_core.messages import AIMessage

        from shared.config import settings

        tid = create_agent(db_conn)
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="1",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", -1)

        response = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": before, "limit": 50},
        )

        assert response.status_code == 200
        assert response.json() == {"items": [], "msg_count": 3, "has_more": False}

    def test_missing_cross_segment_target_is_terminal_even_if_an_older_boundary_exists(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage

        import gateway.routers.timeline as timeline_router
        from shared.config import settings

        tid = create_agent(db_conn)
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="1",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", -1)

        def boundary_ids(_agent_id: int, *, limit: int | None = None) -> list[str]:
            del limit
            return ["missing-newer", "still-older"]

        def missing_segment(_agent_id: int, _checkpoint_id: str) -> list[BaseMessage]:
            return []

        monkeypatch.setattr(timeline_router, "list_compact_boundary_checkpoint_ids", boundary_ids)
        monkeypatch.setattr(timeline_router, "load_checkpoint_messages_segment", missing_segment)

        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": "2.0", "limit": 50},
        )

        assert page.status_code == 200
        assert [item["item_id"] for item in page.json()["items"]] == ["0.0", "1.0"]
        assert page.json()["msg_count"] == 3
        assert page.json()["has_more"] is False

    def test_standing_note_cursor_crosses_from_current_segment(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage, HumanMessage

        from shared.config import settings

        tid = create_agent(db_conn)
        boundary_id = self._put_checkpoint(
            tid, self._segment("history", 1), version="1", boundary=True
        )
        standing_note = HumanMessage(
            content="remember this",
            additional_kwargs={
                "ava_msg_type": "system_note",
                "ava_note_tag": "memory",
                "ava_created_at": "2026-08-25T00:00:01+00:00",
            },
        )
        self._put_checkpoint(
            tid,
            self._current(standing_note, AIMessage(content="current item")),
            version="2",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", 1)

        page = test_client.get(
            f"/api/agents/{tid}/timeline", params={"before": "2.0", "limit": 50}
        ).json()

        assert [item["item_id"] for item in page["items"]] == [
            "0.0",
            "1.0",
            f"s1.{boundary_id}.0.0",
            f"s1.{boundary_id}.1.0",
        ]
        assert page["has_more"] is False

    @pytest.mark.parametrize("damage", ["read_error", "missing", "malformed_message"])
    def test_damaged_or_disappeared_segment_returns_terminal_empty_window(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        damage: str,
    ) -> None:
        from langchain_core.messages import AIMessage

        import gateway.routers.timeline as timeline_router
        from shared.checkpoint import CheckpointReadError
        from shared.config import settings

        tid = create_agent(db_conn)
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="1",
        )
        checkpoint_id = "1f0b9b12-0000-6000-8000-000000000000"
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", -1)

        def boundary_ids(_agent_id: int, *, limit: int | None = None) -> list[str]:
            del limit
            return [checkpoint_id]

        monkeypatch.setattr(timeline_router, "list_compact_boundary_checkpoint_ids", boundary_ids)
        if damage == "missing":

            def missing_segment(_agent_id: int, _checkpoint_id: str) -> list[BaseMessage]:
                return []

            monkeypatch.setattr(
                timeline_router,
                "load_checkpoint_messages_segment",
                missing_segment,
            )
        elif damage == "read_error":

            def fail_read(_agent_id: int, _checkpoint_id: str) -> list[BaseMessage]:
                raise CheckpointReadError("damaged boundary")

            monkeypatch.setattr(timeline_router, "load_checkpoint_messages_segment", fail_read)
        else:

            def malformed_segment(_agent_id: int, _checkpoint_id: str) -> list[BaseMessage]:
                return [
                    HumanMessage(
                        content="damaged inbound",
                        additional_kwargs={
                            "ava_msg_type": "inbound",
                            "ava_inbound_id": "not-an-integer",
                        },
                    )
                ]

            monkeypatch.setattr(
                timeline_router,
                "load_checkpoint_messages_segment",
                malformed_segment,
            )

        response = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{checkpoint_id}.1.0", "limit": 50},
        )

        assert response.status_code == 200
        assert response.json() == {"items": [], "msg_count": 3, "has_more": False}

    def test_long_conversation_keeps_standing_head_notes(
        self, db_conn: psycopg.Connection, test_client: TestClient
    ) -> None:
        """Head context notes (exec timeout / timezone / cluster memory / agent
        id / agent memory — agent/graph/_context_notes.py) fall off the tail
        window in a long conversation; GET must re-attach them right after the
        prompt so the head reads like a fresh window (user report 2026-08-27:
        agent 2992's head showed only "system prompt · compact summary" while a
        fresh agent shows "system prompt · 2 memories · 3 system notes").
        Not counted against `limit`; has_more recomputed."""
        from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        messages: list[BaseMessage] = [SystemMessage(content="You are Ava.")]
        for tag in ("exec_timeout", "timezone", "memory", "agent_id", "agent_memory"):
            messages.append(
                HumanMessage(
                    content=f"[system] {tag} note",
                    additional_kwargs={
                        "ava_msg_type": "system_note",
                        "ava_note_tag": tag,
                        "ava_created_at": "2026-08-27T00:00:00+00:00",
                    },
                )
            )
        for i in range(60):
            messages.append(
                HumanMessage(
                    content=f"user msg {i}",
                    additional_kwargs={
                        "ava_msg_type": "inbound",
                        "ava_created_at": f"2026-08-27T00:01:{i:02d}+00:00",
                        "ava_source": "user",
                    },
                )
            )
        self._put_checkpoint(tid, messages, version="1")

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        # 60 inbounds + 5 notes + 1 system prompt = 66 items; window = 50 + prompt + 5 notes
        assert [item["item_id"] for item in data["items"][:6]] == [
            "0.0",
            "1.0",
            "2.0",
            "3.0",
            "4.0",
            "5.0",
        ]
        assert [item["source"] for item in data["items"][1:6]] == [
            "exec_timeout",
            "timezone",
            "memory",
            "agent_id",
            "agent_memory",
        ]
        assert len(data["items"]) == 56
        # The newest window follows the standing context in order (16.0 is the
        # first item of the 50-item tail).
        assert data["items"][6]["item_id"] == "16.0"
        assert data["items"][-1]["item_id"] == "65.0"
        assert data["has_more"] is True

    def test_short_conversation_head_notes_not_duplicated(
        self, db_conn: psycopg.Connection, test_client: TestClient
    ) -> None:
        """Head notes inside the tail window are not re-attached a second time."""
        from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        messages: list[BaseMessage] = [SystemMessage(content="You are Ava.")]
        for tag in ("exec_timeout", "timezone", "memory", "agent_id", "agent_memory"):
            messages.append(
                HumanMessage(
                    content=f"[system] {tag} note",
                    additional_kwargs={
                        "ava_msg_type": "system_note",
                        "ava_note_tag": tag,
                        "ava_created_at": "2026-08-27T00:00:00+00:00",
                    },
                )
            )
        for i in range(5):
            messages.append(
                HumanMessage(
                    content=f"user msg {i}",
                    additional_kwargs={
                        "ava_msg_type": "inbound",
                        "ava_created_at": f"2026-08-27T00:01:{i:02d}+00:00",
                        "ava_source": "user",
                    },
                )
            )
        self._put_checkpoint(tid, messages, version="1")

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        assert [item["item_id"] for item in data["items"]] == [
            "0.0",
            "1.0",
            "2.0",
            "3.0",
            "4.0",
            "5.0",
            "6.0",
            "7.0",
            "8.0",
            "9.0",
            "10.0",
        ]
        assert data["has_more"] is False

    def test_cursor_past_head_notes_crosses_to_older_segment(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A paging cursor on the first real item past the re-attached head
        notes crosses to the older segment instead of looping on the head."""
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        from shared.config import settings

        tid = create_agent(db_conn)
        boundary_id = self._put_checkpoint(
            tid, self._segment("history", 1), version="1", boundary=True
        )
        notes = [
            HumanMessage(
                content=f"[system] {tag} note",
                additional_kwargs={
                    "ava_msg_type": "system_note",
                    "ava_note_tag": tag,
                    "ava_created_at": "2026-08-27T00:00:00+00:00",
                },
            )
            for tag in ("exec_timeout", "timezone", "memory", "agent_id", "agent_memory")
        ]
        self._put_checkpoint(
            tid,
            [
                SystemMessage(content="system"),
                *notes,
                self._summary("current summary"),
                AIMessage(content="current item"),
            ],
            version="2",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", 1)

        page = test_client.get(
            f"/api/agents/{tid}/timeline", params={"before": "7.0", "limit": 50}
        ).json()

        # The head (prompt + notes + compact summary) is returned once more for
        # frontend de-duplication, then the next older segment's tail follows.
        # The segment's SystemMessage is stripped by the segment loader, so the
        # _segment fixture renders as s1.0.0 (compact summary) + s1.1.0 (chat).
        assert [item["item_id"] for item in page["items"]] == [
            "0.0",
            "1.0",
            "2.0",
            "3.0",
            "4.0",
            "5.0",
            "6.0",
            f"s1.{boundary_id}.0.0",
            f"s1.{boundary_id}.1.0",
        ]
        assert page["has_more"] is False

    def test_historical_head_note_cursor_crosses_to_next_older_segment(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cursor on a historical segment's own standing head note crosses
        to the next older segment. The frontend never guards historical head
        notes (segment-prefixed ids), so the backend cross must recognize them
        as standing context or paging would loop on the segment head (review
        nit, PR #787)."""
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        from shared.config import settings

        tid = create_agent(db_conn)
        boundary_2 = self._put_checkpoint(
            tid, self._segment("oldest", 1), version="1", boundary=True
        )
        notes = [
            HumanMessage(
                content=f"[system] {tag} note",
                additional_kwargs={
                    "ava_msg_type": "system_note",
                    "ava_note_tag": tag,
                    "ava_created_at": "2026-08-27T00:00:00+00:00",
                },
            )
            for tag in ("exec_timeout", "timezone", "memory", "agent_id", "agent_memory")
        ]
        boundary_1 = self._put_checkpoint(
            tid,
            [
                SystemMessage(content="system"),
                *notes,
                self._summary("middle summary"),
                AIMessage(content="middle item"),
            ],
            version="2",
            boundary=True,
        )
        self._put_checkpoint(
            tid,
            [
                SystemMessage(content="system"),
                *notes,
                self._summary("current summary"),
                AIMessage(content="current item"),
            ],
            version="3",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", -1)

        # Cursor on s1's third standing head note (0.0 = exec_timeout,
        # 1.0 = timezone, 2.0 = memory — the segment's SystemMessage is
        # stripped, so its notes start at 0.0): everything before it is
        # standing context, so the response crosses to s2 instead of looping
        # on the segment head.
        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{boundary_1}.2.0", "limit": 50},
        ).json()

        assert [item["item_id"] for item in page["items"]] == [
            f"s1.{boundary_1}.0.0",
            f"s1.{boundary_1}.1.0",
            f"s2.{boundary_2}.0.0",
            f"s2.{boundary_2}.1.0",
        ]
        assert page["has_more"] is False
