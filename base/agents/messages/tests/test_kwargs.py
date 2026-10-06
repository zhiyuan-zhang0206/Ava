"""Contract tests for the typed `ava_*` message-metadata layer
(`base/agents/messages/kwargs.py`).

The load-bearing invariant here is serialization safety: the message
constructors must store the discriminator / note-tag as a **plain `str`**, not
the `StrEnum` member. LangGraph's checkpoint msgpack serializer special-cases
`Enum` (encoding it as a registered custom type and emitting a deprecation
warning on load), so storing a member would silently change the persisted
format for every tagged message. These tests lock the plain-string storage
against that regression.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from base.agents.messages.kwargs import AvaMsgType, NoteTag, read_ava_kwargs


def test_msg_type_value_set() -> None:
    """The discriminator value set is the wire contract — lock it explicitly."""
    assert {t.value for t in AvaMsgType} == {
        "attach",
        "inbound",
        "system_note",
        "exec_output",
        "compact_summary",
        "compact_request",
    }


def test_note_tag_value_set() -> None:
    """The system-note tag set is a cross-stack wire contract."""
    assert {tag.value for tag in NoteTag} == {
        "sdk_hint",
        "agent_reply",
        "task",
        "impersonation",
        "compact_reminder",
        "history_dump",
        "silent_idle_continue",
        "memory",
        "lifecycle_terminate",
        "lifecycle_restart",
        "lifecycle_resurrect",
        "lifecycle_fork",
        "heartbeat",
        "heartbeat_pause",
        "security",
        "context",
        "agent_id",
        "agent_memory",
        "inherited_memory",
        "project_skills",
        "preloaded_skills",
        "new_skills",
        "exec_timeout",
        "timezone",
    }


def test_read_ava_kwargs_is_live_view() -> None:
    """`read_ava_kwargs` returns the message's own additional_kwargs (typed
    reinterpretation, not a copy) so the read side and any writes share it."""
    msg = HumanMessage(content="x", additional_kwargs={"ava_msg_type": "inbound"})
    kw = read_ava_kwargs(msg)
    assert kw is msg.additional_kwargs  # pyright: ignore[reportUnknownMemberType]
    assert kw.get("ava_msg_type") == AvaMsgType.INBOUND


def test_read_time_prefers_picked_up_over_created_at() -> None:
    from langchain_core.messages import AIMessage, HumanMessage

    from base.agents.messages.kwargs import message_read_time

    inbound = HumanMessage(
        content="x",
        additional_kwargs={
            "ava_created_at": "2026-01-01T00:00:00+00:00",
            "ava_picked_up_at": "2026-01-01T00:00:09+00:00",
        },
    )
    ai = AIMessage(content="y", additional_kwargs={"ava_created_at": "2026-01-01T00:00:05+00:00"})
    assert message_read_time(inbound) == "2026-01-01T00:00:09+00:00"
    assert message_read_time(ai) == "2026-01-01T00:00:05+00:00"
    assert message_read_time(HumanMessage(content="legacy")) is None
