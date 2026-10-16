from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.messages import AIMessage

from base.agents.history.timeline import ai_message_items, build_timeline_items
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.kwargs import ExecStatus
from base.clock import Clock
from base.config import settings
from gateway.tests.history.test_timeline import (
    test_client as test_client,
)

_TIMELINE_INPUTS = TimelineReadInputs(
    Clock.from_settings, lambda: settings.general.message_timestamps
)


class TestAiMessageItems:
    def _items(self, msg: AIMessage | str, msg_idx: int = 5) -> list[TimelineItem]:
        from langchain_core.messages import AIMessage

        if not isinstance(msg, AIMessage):
            msg = AIMessage(content=msg)
        return ai_message_items(msg, msg_idx, {})

    def test_string_content_treated_as_chat(self):
        from langchain_core.messages import AIMessage

        items = self._items(AIMessage(content="hello world"))
        assert len(items) == 1
        assert items[0].kind == "agent_chat"
        assert items[0].payload == "hello world"
        assert items[0].item_id == "5.0"

    def test_empty_string_content_no_item(self):
        from langchain_core.messages import AIMessage

        items = self._items(AIMessage(content=""))
        assert items == []

    def test_openai_string_content_plus_tool_call_renders_code(self):
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content="Hi!",
            tool_calls=[{"name": "execute_code", "args": {"code": "go()"}, "id": "o0"}],
        )
        items = self._items(msg)
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
        items = self._items(msg)
        assert [(it.kind, it.payload, it.item_id) for it in items] == [
            ("agent_reasoning", "let me consider", "5.0"),
            ("agent_chat", "the answer is 42", "5.1"),
        ]

    def test_openai_reasoning_block_renders_agent_reasoning(self):
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
        items = self._items(msg)
        assert [(it.kind, it.payload, it.item_id) for it in items] == [
            ("agent_reasoning", "weigh it", "5.0"),
            ("agent_chat", "Day 8.", "5.1"),
            ("agent_code", "print(8)", "5.2"),
        ]

    def test_reasoning_ms_and_tokens_on_first_thinking_item(self):
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
        items = self._items(msg)
        reasoning = items[0]
        assert reasoning.kind == "agent_reasoning"
        assert reasoning.reasoning_ms == 8200
        assert reasoning.reasoning_tokens == 1234
        assert items[1].reasoning_ms is None
        assert items[1].reasoning_tokens is None

    def test_per_block_reasoning_ms_each_thinking_block(self):
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
        items = self._items(msg)
        assert (items[0].reasoning_ms, items[0].reasoning_tokens) == (3000, 500)
        assert (items[1].reasoning_ms, items[1].reasoning_tokens) == (1500, None)

    def test_no_reasoning_metadata_leaves_fields_none(self):
        from langchain_core.messages import AIMessage

        msg = AIMessage(content=[{"type": "thinking", "thinking": "hmm", "index": 0}])
        items = self._items(msg)
        assert items[0].reasoning_ms is None
        assert items[0].reasoning_tokens is None

    def test_exec_ms_surfaces_on_code_output(self):
        from agent.messages import exec_output_message

        msg = exec_output_message(
            content="hello",
            tool_call_id="t1",
            exec_ms=1300,
            status=ExecStatus.COMPLETED,
            body_start=0,
            created_at=datetime.now(UTC),
        )
        items, _ = build_timeline_items([msg], inputs=_TIMELINE_INPUTS)
        assert len(items) == 1
        assert items[0].kind == "code_output"
        assert items[0].exec_ms == 1300

    def test_exec_ms_absent_leaves_field_none(self):
        from langchain_core.messages import ToolMessage

        msg = ToolMessage(
            content="hello",
            tool_call_id="t1",
            additional_kwargs={"ava_msg_type": "exec_output"},
        )
        items, _ = build_timeline_items([msg], inputs=_TIMELINE_INPUTS)
        assert items[0].kind == "code_output"
        assert items[0].exec_ms is None

    def test_system_note_renders_as_system_marker(self):
        from agent.messages import NoteTag, system_note_message

        msg = system_note_message(content="use ava.agents.send_message", tag=NoteTag.AGENT_REPLY)
        items, _ = build_timeline_items([msg], inputs=_TIMELINE_INPUTS)
        assert len(items) == 1
        assert items[0].kind == "system_marker"
        assert items[0].source == "agent_reply"
        assert items[0].payload == "[system] use ava.agents.send_message"

    def test_system_note_show_timestamp_by_tag(self):
        from agent.messages import NoteTag, system_note_message

        shown = {
            NoteTag.HEARTBEAT,
            NoteTag.LIFECYCLE_TERMINATE,
            NoteTag.LIFECYCLE_RESTART,
            NoteTag.LIFECYCLE_RESURRECT,
            NoteTag.LIFECYCLE_FORK,
            NoteTag.NEW_SKILLS,
            NoteTag.TASK,
            NoteTag.IMPERSONATION,
        }
        hidden = {
            NoteTag.MEMORY,
            NoteTag.SKILL_RECALL,
            NoteTag.AGENT_ID,
            NoteTag.COMPACT_REMINDER,
            NoteTag.HISTORY_DUMP,
            NoteTag.SDK_HINT,
            NoteTag.MEMORY_WRITE_CHECK,
            NoteTag.TO_USER_SOFT_LIMIT,
            NoteTag.AGENT_REPLY,
            NoteTag.SILENT_IDLE_CONTINUE,
            NoteTag.SECURITY,
            NoteTag.HEARTBEAT_PAUSE,
            NoteTag.CONTEXT,
            NoteTag.AGENT_ID,
            NoteTag.PROJECT_SKILLS,
            NoteTag.PRELOADED_SKILLS,
            NoteTag.AGENT_MEMORY,
            NoteTag.INHERITED_MEMORY,
            NoteTag.EXEC_TIMEOUT,
            NoteTag.TIMEZONE,
        }
        assert shown | hidden == set(NoteTag)

        for tag in shown:
            items, _ = build_timeline_items(
                [system_note_message(content="x", tag=tag)], inputs=_TIMELINE_INPUTS
            )
            assert items[0].show_timestamp is True, tag
        for tag in hidden:
            items, _ = build_timeline_items(
                [system_note_message(content="x", tag=tag)], inputs=_TIMELINE_INPUTS
            )
            assert items[0].show_timestamp is False, tag

    def test_system_message_renders_as_system_prompt(self):
        from langchain_core.messages import SystemMessage

        msg = SystemMessage(content="You are Ava.\nAct via execute_code.")
        items, msg_count = build_timeline_items([msg], inputs=_TIMELINE_INPUTS)
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
        items = self._items(msg)
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
        items = self._items(msg)
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
        items = self._items(msg)
        assert [(it.kind, it.payload, it.item_id) for it in items] == [
            ("agent_chat", "running tool", "5.0"),
            ("agent_code", "print('hi')", "5.1"),
        ]

    def test_gemini_tool_call_not_in_content_still_renders_code(self):
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content=[{"type": "text", "text": "Let me compute", "index": 0}],
            tool_calls=[{"name": "execute_code", "args": {"code": "print(2 + 3)"}, "id": "g0"}],
        )
        items = self._items(msg)
        assert [(it.kind, it.payload, it.item_id) for it in items] == [
            ("agent_chat", "Let me compute", "5.0"),
            ("agent_code", "print(2 + 3)", "5.1"),
        ]

    def test_gemini_multiple_tool_calls_render_distinct_code_blocks(self):
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content=[],
            tool_calls=[
                {"name": "execute_code", "args": {"code": "print(1)"}, "id": "c0"},
                {"name": "execute_code", "args": {"code": "print(2)"}, "id": "c1"},
                {"name": "execute_code", "args": {"code": "print(3)"}, "id": "c2"},
            ],
        )
        items = self._items(msg)
        assert [(it.kind, it.payload, it.item_id) for it in items] == [
            ("agent_code", "print(1)", "5.0"),
            ("agent_code", "print(2)", "5.1"),
            ("agent_code", "print(3)", "5.2"),
        ]

    def test_per_block_items_not_aggregated(self):
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content=[
                {"type": "thinking", "thinking": "first", "index": 0},
                {"type": "text", "text": "talk", "index": 1},
                {"type": "thinking", "thinking": "second", "index": 2},
            ]
        )
        items = self._items(msg)
        assert [(it.kind, it.payload, it.item_id) for it in items] == [
            ("agent_reasoning", "first", "5.0"),
            ("agent_chat", "talk", "5.1"),
            ("agent_reasoning", "second", "5.2"),
        ]

    def test_empty_list_no_items(self):
        from langchain_core.messages import AIMessage

        items = self._items(AIMessage(content=[]))
        assert items == []
