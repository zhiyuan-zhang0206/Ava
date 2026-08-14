"""Dangling tool_use crash recovery: detection/rebuild helper + both repair passes.

A hard cancel (SIGTERM / restart / stop -> asyncio.CancelledError) can kill the
process after the AIMessage(tool_use) was committed but before exec_node wrote
the paired tool_result. Resuming from that checkpoint sends an API-invalid
message list which Anthropic-compat providers reject with 400 every turn
(agent 167 2026-06-06; agents 236/238 2026-07-13 — buried mid-history shape).

Covers `agent/hooks/repair.py` (shared helper + before_llm hook) and the boot
pass wrapper `agent/startup.py:_repair_dangling_tool_use_at_startup`.
"""

from typing import Any, cast

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.messages.modifier import RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages

from agent.hooks import HOOKS
from agent.hooks.repair import (
    _repair_dangling_tool_use,
    _unpaired_tool_calls,
    dangling_tool_use_repairs,
    register_repair_hooks,
)
from agent.startup import _repair_dangling_tool_use_at_startup


def _ai_tool_use(*ids: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "execute_code", "args": {"code": "1+1"}, "id": i, "type": "tool_call"}
            for i in ids
        ],
    )


# --- pure detection / rebuild function ----------------------------------------


def test_no_repair_for_empty_history() -> None:
    assert dangling_tool_use_repairs([]) == []


def test_no_repair_when_tail_is_paired_tool_message() -> None:
    msgs = [_ai_tool_use("c1"), ToolMessage(content="out", tool_call_id="c1")]
    assert dangling_tool_use_repairs(msgs) == []


def test_no_repair_when_tail_is_human() -> None:
    assert dangling_tool_use_repairs([HumanMessage(content="hi")]) == []


def test_no_repair_when_ai_has_no_tool_calls() -> None:
    assert dangling_tool_use_repairs([AIMessage(content="just text")]) == []


def test_repairs_single_dangling_tool_use_at_tail() -> None:
    repairs = dangling_tool_use_repairs([_ai_tool_use("call_abc")])
    assert len(repairs) == 1
    assert isinstance(repairs[0], ToolMessage)
    assert repairs[0].tool_call_id == "call_abc"
    assert repairs[0].additional_kwargs["ava_cancelled"] is True  # pyright: ignore[reportUnknownMemberType]
    assert repairs[0].additional_kwargs["ava_msg_type"] == "exec_output"  # pyright: ignore[reportUnknownMemberType]


def test_repairs_every_dangling_tool_call_at_tail() -> None:
    repairs = dangling_tool_use_repairs([_ai_tool_use("c1", "c2")])
    assert [r.tool_call_id for r in repairs] == ["c1", "c2"]


def test_partially_paired_tail_appends_only_missing() -> None:
    msgs = [_ai_tool_use("c1", "c2"), ToolMessage(content="out", tool_call_id="c1")]
    repairs = dangling_tool_use_repairs(msgs)
    assert [r.tool_call_id for r in repairs] == ["c2"]
    assert all(isinstance(r, ToolMessage) for r in repairs)


def test_buried_dangling_rebuilds_with_inserted_tool_result() -> None:
    # The 2026-07-13 shape: dangling tool_use buried by claim-appended inbounds.
    msgs = [
        HumanMessage(content="do the thing"),
        _ai_tool_use("call_x"),
        HumanMessage(content="[system] You have been restarted by yourself"),
        HumanMessage(content="[system] Task #26 has not been updated in 1.6h"),
    ]
    repairs = dangling_tool_use_repairs(msgs)
    assert isinstance(repairs[0], RemoveMessage)
    assert repairs[0].id == REMOVE_ALL_MESSAGES
    rebuilt = repairs[1:]
    assert len(rebuilt) == len(msgs) + 1
    # synthetic tool_result lands immediately after the dangling AIMessage
    assert isinstance(rebuilt[2], ToolMessage)
    assert rebuilt[2].tool_call_id == "call_x"
    assert rebuilt[2].additional_kwargs["ava_cancelled"] is True  # pyright: ignore[reportUnknownMemberType]
    # original messages preserved, in order, by identity
    assert [m for m in rebuilt if m is not rebuilt[2]] == msgs


def test_buried_partial_pairing_inserts_after_existing_tool_run() -> None:
    msgs = [
        _ai_tool_use("c1", "c2"),
        ToolMessage(content="out", tool_call_id="c1"),
        HumanMessage(content="later chat"),
    ]
    repairs = dangling_tool_use_repairs(msgs)
    assert isinstance(repairs[0], RemoveMessage)
    rebuilt = repairs[1:]
    # synthetic c2 result inserted after the existing ToolMessage run, before the chat
    assert [type(m).__name__ for m in rebuilt] == [
        "AIMessage",
        "ToolMessage",
        "ToolMessage",
        "HumanMessage",
    ]
    assert rebuilt[2].tool_call_id == "c2"


def test_rebuild_applies_through_real_add_messages_reducer() -> None:
    # Both callers hand the repair value to langgraph's add_messages (boot pass
    # via aupdate_state, hook pass via the node update). Pin the REMOVE_ALL
    # interaction against the real reducer, not a fake: existing messages
    # survive by identity of id/content, the synthetic lands in position, and
    # the merged history is pairing-valid.
    msgs: list[AnyMessage] = [
        HumanMessage(content="do the thing", id="h1"),
        _ai_tool_use("call_x"),
        HumanMessage(content="[system] buried", id="h2"),
    ]
    msgs[1].id = "a1"
    merged = cast(
        "list[AnyMessage]",
        add_messages(msgs, dangling_tool_use_repairs(msgs)),  # type: ignore[arg-type]
    )
    assert [type(m).__name__ for m in merged] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "HumanMessage",
    ]
    assert [m.id for m in merged if not isinstance(m, ToolMessage)] == ["h1", "a1", "h2"]
    synthetic = merged[2]
    assert isinstance(synthetic, ToolMessage)
    assert synthetic.tool_call_id == "call_x"
    assert _unpaired_tool_calls(merged) == []


def test_multiple_buried_danglings_all_repaired() -> None:
    msgs = [
        _ai_tool_use("c1"),
        HumanMessage(content="buried once"),
        _ai_tool_use("c2"),
        HumanMessage(content="buried twice"),
    ]
    repairs = dangling_tool_use_repairs(msgs)
    rebuilt = repairs[1:]
    assert [getattr(m, "tool_call_id", None) for m in rebuilt] == [
        None,
        "c1",
        None,
        None,
        "c2",
        None,
    ]


# --- before_llm hook -----------------------------------------------------------


class _FakeState:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages


_CONFIG = {"configurable": {"thread_id": "236"}}


async def test_hook_returns_none_when_history_valid() -> None:
    state = _FakeState([_ai_tool_use("c1"), ToolMessage(content="o", tool_call_id="c1")])
    assert await _repair_dangling_tool_use(state, None, _CONFIG) is None  # type: ignore[arg-type]


async def test_hook_returns_messages_update_for_buried_dangling() -> None:
    state = _FakeState([_ai_tool_use("c1"), HumanMessage(content="buried")])
    update = await _repair_dangling_tool_use(state, None, _CONFIG)  # type: ignore[arg-type]
    assert update is not None and set(update) == {"messages"}  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(update["messages"][0], RemoveMessage)


def test_register_repair_hooks_registers_before_llm() -> None:
    before = list(HOOKS["before_llm"])
    try:
        register_repair_hooks()
        assert HOOKS["before_llm"][-1] is _repair_dangling_tool_use
    finally:
        HOOKS["before_llm"][:] = before


# --- boot pass over the compiled graph ------------------------------------------


class _FakeSnapshot:
    def __init__(self, messages: list[Any]) -> None:
        self.values = {"messages": messages}


class _FakeGraph:
    """Minimal stand-in for the compiled graph: records aupdate_state calls."""

    def __init__(self, messages: list[Any]) -> None:
        self._messages = messages
        self.updates: list[dict[str, Any]] = []

    async def aget_state(self, _config: Any) -> _FakeSnapshot:
        return _FakeSnapshot(self._messages)

    async def aupdate_state(self, _config: Any, values: dict[str, Any]) -> None:
        self.updates.append(values)


async def test_startup_repair_appends_tool_result_for_dangling() -> None:
    graph = _FakeGraph([_ai_tool_use("call_x")])
    await _repair_dangling_tool_use_at_startup(graph, agent_id=167)  # type: ignore[arg-type]
    assert len(graph.updates) == 1
    repaired = graph.updates[0]["messages"]
    assert repaired[0].tool_call_id == "call_x"


async def test_startup_repair_rebuilds_for_buried_dangling() -> None:
    graph = _FakeGraph([_ai_tool_use("call_x"), HumanMessage(content="buried")])
    await _repair_dangling_tool_use_at_startup(graph, agent_id=236)  # type: ignore[arg-type]
    assert len(graph.updates) == 1
    repaired = graph.updates[0]["messages"]
    assert isinstance(repaired[0], RemoveMessage)
    assert repaired[0].id == REMOVE_ALL_MESSAGES


async def test_startup_repair_is_noop_when_history_valid() -> None:
    graph = _FakeGraph([_ai_tool_use("c1"), ToolMessage(content="o", tool_call_id="c1")])
    await _repair_dangling_tool_use_at_startup(graph, agent_id=1)  # type: ignore[arg-type]
    assert graph.updates == []


async def test_startup_repair_is_noop_for_brand_new_agent() -> None:
    graph = _FakeGraph([])
    await _repair_dangling_tool_use_at_startup(graph, agent_id=2)  # type: ignore[arg-type]
    assert graph.updates == []
