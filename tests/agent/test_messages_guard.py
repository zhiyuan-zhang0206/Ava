"""Message-list mutation hardening — the append-only invariant (task #1256).

Covers the guarded messages reducer (`agent/messages_guard.py`): the three
legal mutation classes (full wipe / tail append / modify-last) pass, every
forbidden shape (edit an older message, reorder, middle insert, delete) is
caught with a structured error, and delta-vs-delta merges (hook runner
co-write) pass through unvalidated.

User ruling (2026-08-13): only ① full delete (compact), ② append, ③ modify
the LLM's last message. Everything else is a violation.
"""

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

from agent.messages_guard import (
    MessagesMutationError,
    guarded_add_messages,
    validate_messages_mutation,
    validate_rebuild,
)


def _msgs(*specs: tuple[str, str]) -> list[AnyMessage]:
    return [HumanMessage(content=c, id=i) for i, c in specs]


# ── Legal class ②: tail append ─────────────────────────────────────────────


def test_append_passes():
    before = _msgs(("a", "hi"), ("b", "there"))
    merged = guarded_add_messages(before, [HumanMessage(content="!", id="c")])
    assert [m.id for m in merged] == ["a", "b", "c"]
    # multiple appends in one delta
    merged = guarded_add_messages(
        before, [HumanMessage(content="!", id="c"), HumanMessage(content="?", id="d")]
    )
    assert [m.id for m in merged] == ["a", "b", "c", "d"]


def test_append_to_empty_history_passes():
    merged = guarded_add_messages([], [HumanMessage(content="hi", id="a")])
    assert [m.id for m in merged] == ["a"]


def test_append_behavior_matches_add_messages():
    """For legal operations the guard must be a drop-in for add_messages."""
    before = _msgs(("a", "hi"), ("b", "there"))
    delta: list[AnyMessage] = [HumanMessage(content="!", id="c")]
    # cast: langgraph's Messages type is invariant over message-likes;
    # the guard accepts the same runtime values (Any signature).
    assert guarded_add_messages(before, delta) == add_messages(cast(Any, before), cast(Any, delta))


# ── Legal class ③: modify the last message (same id) ───────────────────────


def test_modify_last_passes():
    before = _msgs(("a", "hi"), ("b", "there"))
    fixed = HumanMessage(content="there!", id="b")
    merged = guarded_add_messages(before, [fixed])
    assert merged[-1].content == "there!"


def test_modify_last_ai_message_passes():
    """The production shapes: exec's tool-call merge and syntax-fix both
    replace the last AIMessage by same id (model_copy)."""
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


# ── Legal class ①: full wipe ───────────────────────────────────────────────


def test_full_wipe_passes():
    before = _msgs(("a", "hi"), ("b", "there"))
    merged = guarded_add_messages(before, [RemoveMessage(id=REMOVE_ALL_MESSAGES)])
    assert merged == []


def test_wipe_then_rebuild_with_unchanged_survivors_passes():
    """The crash-repair shape (agent/hooks/repair.py): REMOVE_ALL rebuild
    re-lists the old messages unchanged and splices synthetic tool_results
    mid-list — survivors keep content and relative order, new messages may
    appear anywhere."""
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
    """The hook runner merges sibling hook deltas through the same reducer —
    `current` is itself a delta (carries markers) and carries no invariant;
    the guard must pass it through with plain add_messages semantics."""
    compact_delta = [RemoveMessage(id=REMOVE_ALL_MESSAGES)]
    note = [HumanMessage(content="note", id="n1")]
    merged = guarded_add_messages(compact_delta, note)
    # add_messages only honors REMOVE_ALL found in `right`; the marker in
    # `current` must survive the co-write merge so the commit-side reducer
    # still sees it — the guard passes the delta through unvalidated.
    assert merged == [RemoveMessage(id=REMOVE_ALL_MESSAGES), note[0]]


# ── Forbidden: edit an earlier message ─────────────────────────────────────


def test_edit_middle_message_raises():
    before = _msgs(("a", "hi"), ("b", "there"), ("c", "friend"))
    edited = HumanMessage(content="CHANGED", id="b")
    with pytest.raises(MessagesMutationError) as ei:
        guarded_add_messages(before, [edited])
    msg = str(ei.value)
    assert "index 1" in msg and "HumanMessage" in msg


def test_edit_second_to_last_raises():
    """Only the LAST message may change — the second-to-last is still an
    earlier message."""
    before = _msgs(("a", "hi"), ("b", "there"), ("c", "friend"))
    with pytest.raises(MessagesMutationError):
        guarded_add_messages(before, [HumanMessage(content="CHANGED", id="b")])


# ── Forbidden: reorder / middle insert / delete ────────────────────────────


def test_reorder_raises():
    before = _msgs(("a", "1"), ("b", "2"), ("c", "3"))
    after = _msgs(("a", "1"), ("c", "3"), ("b", "2"))
    with pytest.raises(MessagesMutationError) as ei:
        validate_messages_mutation(before, after)
    assert "moved" in str(ei.value)


def test_middle_insert_raises():
    """A new message between survivors — impossible via plain add_messages
    (it can only append or replace-by-id), but the validator must catch the
    shape defensively."""
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
    after = [before[0], before[2]]  # b gone, c shifted
    with pytest.raises(MessagesMutationError):
        validate_messages_mutation(before, after)


def test_targeted_removal_via_reducer_raises():
    """RemoveMessage(id=...) of an existing older message is a deletion —
    forbidden (only REMOVE_ALL may delete)."""
    before = _msgs(("a", "1"), ("b", "2"), ("c", "3"))
    with pytest.raises(MessagesMutationError):
        guarded_add_messages(before, [RemoveMessage(id="a")])


def test_delete_last_and_append_new_raises():
    """Delete the last message + append a replacement (different id) is a
    deletion — the ruling allows *modifying* the last message, not deleting
    it; message ids are never reallocated (task #1256 audit)."""
    before = _msgs(("a", "1"), ("b", "2"), ("c", "3"))
    with pytest.raises(MessagesMutationError):
        guarded_add_messages(before, [RemoveMessage(id="c"), HumanMessage(content="new", id="d")])


# ── Forbidden: rebuild tampering ───────────────────────────────────────────


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


# ── Pure rebuild validator ─────────────────────────────────────────────────


def test_validate_rebuild_accepts_repair_shape():
    before = [HumanMessage(content="q", id="h1"), AIMessage(content="", id="a1", tool_calls=[])]
    after = [before[0], before[1], ToolMessage(content="[interrupted]", tool_call_id="x", id="tr1")]
    validate_rebuild(before, after)  # must not raise
