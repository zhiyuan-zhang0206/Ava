from __future__ import annotations

from langchain_core.messages import HumanMessage

from base.agents.messages.kwargs import AvaMsgType, NoteTag, read_ava_kwargs


def test_msg_type_value_set() -> None:
    assert {t.value for t in AvaMsgType} == {
        "attach",
        "inbound",
        "system_note",
        "exec_output",
        "compact_summary",
        "compact_request",
    }


def test_note_tag_value_set() -> None:
    assert {tag.value for tag in NoteTag} == {
        "sdk_hint",
        "agent_reply",
        "task",
        "impersonation",
        "compact_reminder",
        "history_dump",
        "silent_idle_continue",
        "memory",
        "memory_write_check",
        "to_user_soft_limit",
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
        "skill_recall",
        "exec_timeout",
        "timezone",
    }


def test_read_ava_kwargs_is_live_view() -> None:
    msg = HumanMessage(content="x", additional_kwargs={"ava_msg_type": "inbound"})
    kw = read_ava_kwargs(msg)
    assert kw is msg.additional_kwargs
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
