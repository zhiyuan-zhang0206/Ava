"""Timeline SDK call projection contracts."""

from __future__ import annotations

from base.agents.history.timeline import (
    _ai_message_items,
    build_timeline_items,
)
from base.agents.messages.kwargs import ExecStatus
from gateway.agents.history.tests.test_timeline import (
    test_client as test_client,
)


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
        return _ai_message_items(msg, msg_idx, self._next_ts, {})  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

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
        the normalized tool_calls. base.lm.reasoning folds reasoning → thinking,
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

        msg = exec_output_message(
            content="hello",
            tool_call_id="t1",
            exec_ms=1300,
            status=ExecStatus.COMPLETED,
            body_start=0,
        )
        items, _ = build_timeline_items([msg], [])
        assert len(items) == 1
        assert items[0].kind == "code_output"
        assert items[0].exec_ms == 1300

    def test_exec_ms_absent_leaves_field_none(self):
        """A historical exec_output checkpoint without ava_exec_ms keeps exec_ms
        None — the chip then just shows the line count."""
        from langchain_core.messages import ToolMessage

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
            NoteTag.IMPERSONATION,
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
            # Inherited memory is standing context (the ancestor chain's
            # blocks), same family as the memory notes above.
            NoteTag.INHERITED_MEMORY,
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
