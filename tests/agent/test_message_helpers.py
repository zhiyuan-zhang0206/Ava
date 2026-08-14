"""agent/messages.py — pure unit tests for helpers.

No DB / LangGraph state needed — just construct messages and verify metadata shape.
"""

from langchain_core.messages import HumanMessage

from agent.messages import inbound_message


class TestInboundMessageMetadata:
    """`inbound_message` helper additional_kwargs injection — the read side (timeline
    endpoint / hook) classifies by ava_msg_type and ava_source; any typo in
    key/value causes dispatch to silently mis-classify. mutmut exposed that this
    helper had zero unit tests before (PR #290); add explicit shape assertions to
    lock down the metadata."""

    def test_returns_humanmessage(self):
        msg = inbound_message(content="hi", source="user", inbound_id=1)
        assert isinstance(msg, HumanMessage)

    def test_content_passes_through(self):
        msg = inbound_message(content="hello world", source="user", inbound_id=2)
        assert msg.content == "hello world"  # pyright: ignore[reportUnknownMemberType]

    def test_additional_kwargs_exact_shape(self):
        """Full metadata shape — locks key names / value casing / source + inbound_id passthrough."""
        msg = inbound_message(content="x", source="agent:42", inbound_id=3)
        assert msg.additional_kwargs == {  # pyright: ignore[reportUnknownMemberType]
            "ava_msg_type": "inbound",
            "ava_source": "agent:42",
            "ava_inbound_id": 3,
        }

    def test_source_value_propagates_distinct_inputs(self):
        """Different sources go into the same helper, all accurately reflected (guards against hardcoded source bugs)."""
        for src in ("user", "agent:5", "system", "watcher:3"):
            msg = inbound_message(content="x", source=src, inbound_id=1)
            assert msg.additional_kwargs["ava_source"] == src  # pyright: ignore[reportUnknownMemberType]


class TestMessageCreatedAtStamp:
    """All three message constructors stamp `ava_created_at` (ISO-8601) when
    handed the message's real creation time, and omit the key when not — the
    timeline read side prefers this real ts over its synthetic anchor+offset
    fallback (so an agent's own messages no longer render at 1970 / a stale
    chat's time)."""

    def test_inbound_message_stamps_when_given(self):
        from datetime import UTC, datetime

        dt = datetime(2026, 6, 19, 15, 30, tzinfo=UTC)
        msg = inbound_message(content="hi", source="user", inbound_id=1, created_at=dt)
        assert msg.additional_kwargs["ava_created_at"] == "2026-06-19T15:30:00+00:00"  # pyright: ignore[reportUnknownMemberType]

    def test_inbound_message_omits_when_absent(self):
        msg = inbound_message(content="hi", source="user", inbound_id=1)
        assert "ava_created_at" not in msg.additional_kwargs  # pyright: ignore[reportUnknownMemberType]

    def test_exec_output_message_stamps_when_given(self):
        from datetime import UTC, datetime

        from agent.messages import exec_output_message

        dt = datetime(2026, 6, 19, 15, 30, tzinfo=UTC)
        msg = exec_output_message(content="out", tool_call_id="t1", exit_code=0, created_at=dt)
        assert msg.additional_kwargs["ava_created_at"] == "2026-06-19T15:30:00+00:00"  # pyright: ignore[reportUnknownMemberType]

    def test_system_note_message_stamps_when_given(self):
        from datetime import UTC, datetime

        from agent.messages import NoteTag, system_note_message

        dt = datetime(2026, 6, 19, 15, 30, tzinfo=UTC)
        msg = system_note_message(content="note", tag=NoteTag.MEMORY, created_at=dt)
        assert msg.additional_kwargs["ava_created_at"] == "2026-06-19T15:30:00+00:00"  # pyright: ignore[reportUnknownMemberType]


# ── has_conversation ──
# The standing head (SystemMessage + context notes) is laid down by
# `init_context` before claim ever runs, so "nothing has happened yet" cannot be
# `not messages`. Claim's idle-vs-continue predicate rides on this: reading a
# fresh window as a multi-step loop spends an LLM turn on an empty conversation.


def test_has_conversation_false_for_a_freshly_established_window() -> None:
    from langchain_core.messages import AnyMessage, SystemMessage

    from agent.messages import NoteTag, has_conversation, system_note_message

    head: list[AnyMessage] = [
        SystemMessage(content="<prompt>"),
        system_note_message(content="your id", tag=NoteTag.AGENT_ID),
        system_note_message(content="the index", tag=NoteTag.MEMORY),
        system_note_message(content="your memory", tag=NoteTag.AGENT_MEMORY),
    ]
    assert has_conversation(head) is False


def test_has_conversation_false_for_an_empty_window() -> None:
    from agent.messages import has_conversation

    assert has_conversation([]) is False


def test_has_conversation_true_once_an_inbound_lands() -> None:
    from langchain_core.messages import AnyMessage, SystemMessage

    from agent.messages import NoteTag, has_conversation, inbound_message, system_note_message

    msgs: list[AnyMessage] = [
        SystemMessage(content="<prompt>"),
        system_note_message(content="your id", tag=NoteTag.AGENT_ID),
        inbound_message(content="hello", source="user", inbound_id=1),
    ]
    assert has_conversation(msgs) is True


def test_has_conversation_true_for_an_agent_reply() -> None:
    from langchain_core.messages import AIMessage, AnyMessage, SystemMessage

    from agent.messages import NoteTag, has_conversation, system_note_message

    msgs: list[AnyMessage] = [
        SystemMessage(content="<prompt>"),
        system_note_message(content="your id", tag=NoteTag.AGENT_ID),
        AIMessage(content="on it"),
    ]
    assert has_conversation(msgs) is True


def test_has_conversation_true_for_a_post_compact_summary() -> None:
    """A compacted window is a conversation in progress: the summary is what the
    agent carries forward, so claim must continue rather than block."""
    from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

    from agent.messages import NoteTag, has_conversation, system_note_message

    msgs: list[AnyMessage] = [
        SystemMessage(content="<prompt>"),
        system_note_message(content="your id", tag=NoteTag.AGENT_ID),
        HumanMessage(content="[system] Your context was just compacted. ..."),
    ]
    assert has_conversation(msgs) is True
