from typing import Any, cast

import pytest
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages

from agent.messages.guard import (
    MessagesMutationError,
    guarded_add_messages,
    validate_messages_mutation,
    validate_rebuild,
)


def _msgs(*specs: tuple[str, str]) -> list[AnyMessage]:
    return [HumanMessage(content=c, id=i) for i, c in specs]


def test_append_passes():
    before = _msgs(("a", "hi"), ("b", "there"))
    merged = guarded_add_messages(before, [HumanMessage(content="!", id="c")])
    assert [m.id for m in merged] == ["a", "b", "c"]
    merged = guarded_add_messages(
        before, [HumanMessage(content="!", id="c"), HumanMessage(content="?", id="d")]
    )
    assert [m.id for m in merged] == ["a", "b", "c", "d"]


def test_append_to_empty_history_passes():
    merged = guarded_add_messages([], [HumanMessage(content="hi", id="a")])
    assert [m.id for m in merged] == ["a"]


def test_append_behavior_matches_add_messages():
    before = _msgs(("a", "hi"), ("b", "there"))
    delta: list[AnyMessage] = [HumanMessage(content="!", id="c")]
    assert guarded_add_messages(before, delta) == add_messages(cast(Any, before), cast(Any, delta))


def test_modify_last_passes():
    before = _msgs(("a", "hi"), ("b", "there"))
    fixed = HumanMessage(content="there!", id="b")
    merged = guarded_add_messages(before, [fixed])
    assert merged[-1].content == "there!"


def test_modify_last_ai_message_passes():
    before = [
        HumanMessage(content="hi", id="a"),
        AIMessage(content="", id="b", tool_calls=[]),
    ]
    fixed = AIMessage(content="", id="b", tool_calls=[])
    fixed.tool_calls = [
        {"name": "execute_code", "args": {"code": "x"}, "id": "t1", "type": "tool_call"}
    ]
    merged = guarded_add_messages(before, [fixed])
    assert merged[-1].tool_calls[0]["args"]["code"] == "x"


def test_full_wipe_passes():
    before = _msgs(("a", "hi"), ("b", "there"))
    merged = guarded_add_messages(before, [RemoveMessage(id=REMOVE_ALL_MESSAGES)])
    assert merged == []


def test_wipe_then_rebuild_with_unchanged_survivors_passes():
    before = [
        HumanMessage(content="q", id="h1"),
        AIMessage(
            content="",
            id="a1",
            tool_calls=[
                {"name": "execute_code", "args": {"code": "x"}, "id": "t1", "type": "tool_call"}
            ],
        ),
        HumanMessage(content="note", id="n1"),
    ]
    repaired = [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        before[0],
        before[1],
        ToolMessage(content="[interrupted]", tool_call_id="t1", id="tr1"),
        before[2],
    ]
    merged = guarded_add_messages(before, repaired)
    assert [m.id for m in merged] == ["h1", "a1", "tr1", "n1"]


def test_hook_cowrite_delta_merge_passes_unvalidated():
    compact_delta = [RemoveMessage(id=REMOVE_ALL_MESSAGES)]
    note = [HumanMessage(content="note", id="n1")]
    merged = guarded_add_messages(compact_delta, note)
    assert merged == [RemoveMessage(id=REMOVE_ALL_MESSAGES), note[0]]


def test_edit_middle_message_raises():
    before = _msgs(("a", "hi"), ("b", "there"), ("c", "friend"))
    edited = HumanMessage(content="CHANGED", id="b")
    with pytest.raises(MessagesMutationError) as ei:
        guarded_add_messages(before, [edited])
    msg = str(ei.value)
    assert "index 1" in msg and "HumanMessage" in msg


def test_edit_second_to_last_raises():
    before = _msgs(("a", "hi"), ("b", "there"), ("c", "friend"))
    with pytest.raises(MessagesMutationError):
        guarded_add_messages(before, [HumanMessage(content="CHANGED", id="b")])


def test_reorder_raises():
    before = _msgs(("a", "1"), ("b", "2"), ("c", "3"))
    after = _msgs(("a", "1"), ("c", "3"), ("b", "2"))
    with pytest.raises(MessagesMutationError) as ei:
        validate_messages_mutation(before, after)
    assert "moved" in str(ei.value)


def test_middle_insert_raises():
    before = _msgs(("a", "1"), ("b", "2"), ("c", "3"))
    after = [
        before[0],
        HumanMessage(content="sneaky", id="x"),
        before[1],
        before[2],
    ]
    with pytest.raises(MessagesMutationError) as ei:
        validate_messages_mutation(before, after)
    assert "tail suffix" in str(ei.value)


def test_delete_tail_message_raises():
    before = _msgs(("a", "1"), ("b", "2"), ("c", "3"))
    with pytest.raises(MessagesMutationError) as ei:
        validate_messages_mutation(before, before[:-1])
    assert "deleted" in str(ei.value)


def test_delete_middle_message_raises():
    before = _msgs(("a", "1"), ("b", "2"), ("c", "3"))
    after = [before[0], before[2]]
    with pytest.raises(MessagesMutationError):
        validate_messages_mutation(before, after)


def test_targeted_removal_via_reducer_raises():
    before = _msgs(("a", "1"), ("b", "2"), ("c", "3"))
    with pytest.raises(MessagesMutationError):
        guarded_add_messages(before, [RemoveMessage(id="a")])


def test_delete_last_and_append_new_raises():
    before = _msgs(("a", "1"), ("b", "2"), ("c", "3"))
    with pytest.raises(MessagesMutationError):
        guarded_add_messages(before, [RemoveMessage(id="c"), HumanMessage(content="new", id="d")])


def test_rebuild_altering_survivor_raises():
    before = _msgs(("a", "1"), ("b", "2"))
    tampered = [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        HumanMessage(content="1 EDITED", id="a"),
        before[1],
    ]
    with pytest.raises(MessagesMutationError) as ei:
        guarded_add_messages(before, tampered)
    assert "altered surviving" in str(ei.value)


def test_rebuild_reordering_survivors_raises():
    before = _msgs(("a", "1"), ("b", "2"), ("c", "3"))
    reordered = [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        before[2],
        before[0],
        before[1],
    ]
    with pytest.raises(MessagesMutationError) as ei:
        guarded_add_messages(before, reordered)
    assert "reordered" in str(ei.value)


def test_validate_rebuild_accepts_repair_shape():
    before = [HumanMessage(content="q", id="h1"), AIMessage(content="", id="a1", tool_calls=[])]
    after = [before[0], before[1], ToolMessage(content="[interrupted]", tool_call_id="x", id="tr1")]
    validate_rebuild(before, after)
