from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from base.agents.history.timeline import TimelineItem, build_timeline_items
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.agents.messages.kwargs import AvaMsgType, NoteTag
from base.clock import Clock
from base.config import settings

_INPUTS = TimelineReadInputs(Clock.from_settings, lambda: settings.general.message_timestamps)


def _render(*messages: BaseMessage) -> list[TimelineItem]:
    items, count = build_timeline_items(list(messages), inputs=_INPUTS)
    assert count == len(messages)
    return items


def test_every_message_shape_without_a_read_time_renders_with_none() -> None:
    items = _render(
        HumanMessage(
            content="hello",
            additional_kwargs={"ava_msg_type": AvaMsgType.INBOUND.value, "ava_source": "user"},
        ),
        AIMessage(
            content=[
                {"type": "thinking", "thinking": "hmm", "index": 0},
                {"type": "text", "text": "done", "index": 1},
            ],
            tool_calls=[{"name": "execute_code", "args": {"code": "1"}, "id": "c1"}],
        ),
        ToolMessage(
            content="out",
            tool_call_id="c1",
            additional_kwargs={"ava_msg_type": AvaMsgType.EXEC_OUTPUT.value},
        ),
        HumanMessage(
            content="note",
            additional_kwargs={
                "ava_msg_type": AvaMsgType.SYSTEM_NOTE.value,
                "ava_note_tag": NoteTag.MEMORY.value,
            },
        ),
        HumanMessage(content="attached", additional_kwargs={"ava_msg_type": "attach"}),
        HumanMessage(content="plain"),
    )
    assert [item.kind for item in items] == [
        "inbound_chat",
        "agent_reasoning",
        "agent_chat",
        "agent_code",
        "code_output",
        "system_marker",
        "attach",
        "system_marker",
    ]
    assert [item.created_at for item in items] == [None] * len(items)


def test_an_inbound_without_an_embedded_id_renders_no_inbound_id() -> None:
    (item,) = _render(
        HumanMessage(
            content="hello",
            additional_kwargs={
                "ava_msg_type": AvaMsgType.INBOUND.value,
                "ava_created_at": "2026-10-13T08:00:00+00:00",
            },
        )
    )
    assert item.inbound_id is None
    assert item.created_at == "2026-10-13T08:00:00+00:00"


def test_the_read_time_is_the_pickup_time_else_the_creation_time() -> None:
    picked, created = _render(
        HumanMessage(
            content="picked up",
            additional_kwargs={
                "ava_msg_type": AvaMsgType.INBOUND.value,
                "ava_created_at": "2026-10-13T08:00:00+00:00",
                "ava_picked_up_at": "2026-10-13T08:00:05+00:00",
            },
        ),
        AIMessage(
            content="answer", additional_kwargs={"ava_created_at": "2026-10-13T08:00:09+00:00"}
        ),
    )
    assert picked.created_at == "2026-10-13T08:00:05+00:00"
    assert created.created_at == "2026-10-13T08:00:09+00:00"


def test_a_mixed_window_keeps_each_message_its_own_time() -> None:
    stamped, missing = _render(
        AIMessage(content="old", additional_kwargs={"ava_created_at": "2026-10-13T08:00:00+00:00"}),
        AIMessage(content="older still, never stamped"),
    )
    assert stamped.created_at == "2026-10-13T08:00:00+00:00"
    assert missing.created_at is None
