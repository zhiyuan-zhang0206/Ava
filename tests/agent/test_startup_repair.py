"""Dangling tool pairing crash recovery: detection/rebuild helper + both repair passes.

A hard cancel (SIGTERM / restart / stop -> asyncio.CancelledError) can leave an
AIMessage(tool_use) without its ToolMessage, or a ToolMessage without a
preceding AIMessage(tool_use). Resuming either API-invalid history causes
Anthropic-compat providers to reject every turn with 400 (agent 167 2026-06-06;
agents 236/238 2026-07-13; agent 5333 2026-08-31).

Covers `agent/hooks/repair.py` (shared helper + before_llm hook) and the boot
pass wrapper `agent/startup.py:_repair_dangling_tool_use_at_startup`.
"""

from typing import Any, cast
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.messages.modifier import RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages

from agent.hooks import HOOKS
from agent.hooks import repair as repair_module
from agent.hooks.repair import (
    _orphan_tool_results,
    _repair_dangling_tool_pairing,
    _unpaired_tool_calls,
    dangling_tool_pairing_repairs,
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


def _full_rebuild(repairs: list[Any]) -> list[Any]:
    """Assert the only allowed repair shape and return its rebuilt history."""
    assert isinstance(repairs[0], RemoveMessage)
    assert repairs[0].id == REMOVE_ALL_MESSAGES
    return repairs[1:]


# --- pure detection / rebuild function ----------------------------------------


def test_no_repair_for_empty_history() -> None:
    assert dangling_tool_pairing_repairs([]) == []


def test_no_repair_when_tail_is_paired_tool_message() -> None:
    msgs = [_ai_tool_use("c1"), ToolMessage(content="out", tool_call_id="c1")]
    assert dangling_tool_pairing_repairs(msgs) == []


def test_no_repair_when_tail_is_human() -> None:
    assert dangling_tool_pairing_repairs([HumanMessage(content="hi")]) == []


def test_no_repair_when_ai_has_no_tool_calls() -> None:
    assert dangling_tool_pairing_repairs([AIMessage(content="just text")]) == []


def test_orphan_detector_scans_all_preceding_ai_messages() -> None:
    """Only pairing adjacency is invalid here; the result itself is not orphaned."""
    messages = [
        _ai_tool_use("c1"),
        HumanMessage(content="intervening message"),
        ToolMessage(content="out", tool_call_id="c1"),
    ]
    assert _orphan_tool_results(messages) == []


def test_repair_tolerates_empty_tool_call_id_and_drops_other_orphan() -> None:
    """An empty normalized id pairs with an empty result without killing recovery."""
    empty_id_use = AIMessage(
        content="",
        tool_calls=[
            {"name": "execute_code", "args": {"code": "1+1"}, "id": None, "type": "tool_call"}
        ],
    )
    empty_id_result = ToolMessage(content="out", tool_call_id="", id="t-empty")
    orphan = ToolMessage(content="stale", tool_call_id="other", id="t-orphan")
    messages: list[AnyMessage] = [empty_id_use, empty_id_result, orphan]

    repairs = dangling_tool_pairing_repairs(messages)

    assert _orphan_tool_results(messages) == [2]
    rebuilt = _full_rebuild(repairs)
    assert rebuilt == [empty_id_use, empty_id_result]
    assert rebuilt[0] is empty_id_use
    assert rebuilt[1] is empty_id_result


def test_repairs_single_dangling_tool_use_at_tail() -> None:
    original = _ai_tool_use("call_abc")
    rebuilt = _full_rebuild(dangling_tool_pairing_repairs([original]))
    assert rebuilt[0] is original
    assert isinstance(rebuilt[1], ToolMessage)
    assert rebuilt[1].tool_call_id == "call_abc"
    assert rebuilt[1].additional_kwargs["ava_cancelled"] is True  # pyright: ignore[reportUnknownMemberType]
    assert rebuilt[1].additional_kwargs["ava_msg_type"] == "exec_output"  # pyright: ignore[reportUnknownMemberType]


def test_repairs_every_dangling_tool_call_at_tail() -> None:
    original = _ai_tool_use("c1", "c2")
    rebuilt = _full_rebuild(dangling_tool_pairing_repairs([original]))
    assert rebuilt[0] is original
    assert [r.tool_call_id for r in rebuilt[1:]] == ["c1", "c2"]


def test_partially_paired_tail_appends_only_missing() -> None:
    original = _ai_tool_use("c1", "c2")
    existing = ToolMessage(content="out", tool_call_id="c1")
    rebuilt = _full_rebuild(dangling_tool_pairing_repairs([original, existing]))
    assert rebuilt[:2] == [original, existing]
    assert rebuilt[0] is original
    assert rebuilt[1] is existing
    assert isinstance(rebuilt[2], ToolMessage)
    assert rebuilt[2].tool_call_id == "c2"


def test_buried_dangling_rebuilds_with_inserted_tool_result() -> None:
    # The 2026-07-13 shape: dangling tool_use buried by claim-appended inbounds.
    msgs = [
        HumanMessage(content="do the thing"),
        _ai_tool_use("call_x"),
        HumanMessage(content="[system] You have been restarted by yourself"),
        HumanMessage(content="[system] Task #26 has not been updated in 1.6h"),
    ]
    repairs = dangling_tool_pairing_repairs(msgs)
    rebuilt = _full_rebuild(repairs)
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
    repairs = dangling_tool_pairing_repairs(msgs)
    rebuilt = _full_rebuild(repairs)
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
        add_messages(msgs, dangling_tool_pairing_repairs(msgs)),  # type: ignore[arg-type]
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


def test_orphan_tool_result_at_tail_rebuilds_and_drops_it_through_reducer() -> None:
    """A tool_result with no preceding tool_use is removed without altering survivors."""
    prefix = HumanMessage(content="before", id="h1")
    thinking = AIMessage(content="thinking", id="a1")
    orphan = ToolMessage(content="stale output", tool_call_id="orphan", id="t1")
    messages: list[AnyMessage] = [prefix, thinking, orphan]

    repairs = dangling_tool_pairing_repairs(messages)
    rebuilt = _full_rebuild(repairs)
    assert rebuilt == [prefix, thinking]
    assert rebuilt[0] is prefix
    assert rebuilt[1] is thinking

    merged = cast(
        "list[AnyMessage]",
        add_messages(messages, repairs),  # type: ignore[arg-type]
    )
    assert merged == [prefix, thinking]
    assert merged[0] is prefix
    assert merged[1] is thinking


def test_orphan_tool_result_buried_in_5333_shape_is_dropped() -> None:
    """A later HumanMessage does not make a prior orphan tool_result valid."""
    messages: list[AnyMessage] = [
        HumanMessage(content="earlier", id="h1"),
        AIMessage(content="thinking", id="a1"),
        HumanMessage(content="now restart", id="h2"),
        ToolMessage(content="lost result", tool_call_id="KaN9", id="t1"),
    ]

    repairs = dangling_tool_pairing_repairs(messages)
    rebuilt = _full_rebuild(repairs)
    assert rebuilt == messages[:-1]
    assert all(actual is expected for actual, expected in zip(rebuilt, messages[:-1], strict=True))


def test_orphan_and_dangling_tool_use_are_repaired_in_one_atomic_rebuild() -> None:
    """Dropping an orphan and synthesizing an interrupted pair share one channel update."""
    use = _ai_tool_use("c1")
    use.id = "a1"
    orphan = ToolMessage(content="wrong result", tool_call_id="orphan", id="t1")
    later = HumanMessage(content="now restart", id="h1")
    messages: list[AnyMessage] = [use, orphan, later]

    repairs = dangling_tool_pairing_repairs(messages)
    rebuilt = _full_rebuild(repairs)
    assert rebuilt[0] is use
    assert isinstance(rebuilt[1], ToolMessage)
    assert rebuilt[1].tool_call_id == "c1"
    assert rebuilt[2] is later

    merged = cast(
        "list[AnyMessage]",
        add_messages(messages, repairs),  # type: ignore[arg-type]
    )
    assert _unpaired_tool_calls(merged) == []
    assert repair_module._orphan_tool_results(merged) == []


def test_5333_kill_mid_exec_repair_converges_after_one_rebuild() -> None:
    """The lost tool_use/result pair is restored atomically and is idempotent."""
    restart = HumanMessage(content="now restart", id="h1")
    use = _ai_tool_use("KaN9")
    use.id = "a1"
    messages: list[AnyMessage] = [restart, use]

    repairs = dangling_tool_pairing_repairs(messages)
    rebuilt = _full_rebuild(repairs)
    assert rebuilt[0] is restart
    assert rebuilt[1] is use
    assert isinstance(rebuilt[2], ToolMessage)
    assert rebuilt[2].tool_call_id == "KaN9"

    merged = cast("list[AnyMessage]", add_messages(messages, repairs))  # type: ignore[arg-type]
    assert _unpaired_tool_calls(merged) == []
    assert repair_module._orphan_tool_results(merged) == []
    assert dangling_tool_pairing_repairs(merged) == []


def test_multiple_buried_danglings_all_repaired() -> None:
    msgs = [
        _ai_tool_use("c1"),
        HumanMessage(content="buried once"),
        _ai_tool_use("c2"),
        HumanMessage(content="buried twice"),
    ]
    repairs = dangling_tool_pairing_repairs(msgs)
    rebuilt = _full_rebuild(repairs)
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
    assert await _repair_dangling_tool_pairing(state, None, _CONFIG) is None  # type: ignore[arg-type]


async def test_hook_returns_messages_update_for_buried_dangling() -> None:
    state = _FakeState([_ai_tool_use("c1"), HumanMessage(content="buried")])
    update = await _repair_dangling_tool_pairing(state, None, _CONFIG)  # type: ignore[arg-type]
    assert update is not None and set(update) == {"messages"}  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(update["messages"][0], RemoveMessage)


def test_register_repair_hooks_registers_before_llm() -> None:
    before = list(HOOKS["before_llm"])
    try:
        register_repair_hooks()
        assert HOOKS["before_llm"][-1] is _repair_dangling_tool_pairing
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
        self.checkpointer = object()

    async def aget_state(self, _config: Any) -> _FakeSnapshot:
        return _FakeSnapshot(self._messages)

    async def aupdate_state(self, _config: Any, values: dict[str, Any]) -> None:
        self.updates.append(values)


async def test_startup_repair_rebuilds_tool_result_for_dangling() -> None:
    graph = _FakeGraph([_ai_tool_use("call_x")])
    await _repair_dangling_tool_use_at_startup(graph, agent_id=167)  # type: ignore[arg-type]
    assert len(graph.updates) == 1
    repaired = graph.updates[0]["messages"]
    assert isinstance(repaired[0], RemoveMessage)
    assert isinstance(repaired[2], ToolMessage)
    assert repaired[2].tool_call_id == "call_x"


async def test_startup_repair_rebuilds_for_buried_dangling() -> None:
    graph = _FakeGraph([_ai_tool_use("call_x"), HumanMessage(content="buried")])
    await _repair_dangling_tool_use_at_startup(graph, agent_id=236)  # type: ignore[arg-type]
    assert len(graph.updates) == 1
    repaired = graph.updates[0]["messages"]
    assert isinstance(repaired[0], RemoveMessage)
    assert repaired[0].id == REMOVE_ALL_MESSAGES


async def test_startup_repair_drops_orphan_tool_result_with_rebuild() -> None:
    orphan = ToolMessage(content="lost result", tool_call_id="KaN9", id="t1")
    graph = _FakeGraph([HumanMessage(content="now restart", id="h1"), orphan])

    await _repair_dangling_tool_use_at_startup(graph, agent_id=5333)  # type: ignore[arg-type]

    assert len(graph.updates) == 1
    repaired = graph.updates[0]["messages"]
    assert isinstance(repaired[0], RemoveMessage)
    assert repaired[0].id == REMOVE_ALL_MESSAGES
    assert repaired[1].content == "now restart"
    assert all(not isinstance(message, ToolMessage) for message in repaired[1:])


async def test_startup_repair_flushes_a_skipped_checkpoint_update() -> None:
    graph = _FakeGraph([_ai_tool_use("call_x")])
    flush = AsyncMock()
    graph.checkpointer = type("_NstepSaver", (), {"_ava_nstep_flush": flush})()

    await _repair_dangling_tool_use_at_startup(graph, agent_id=167)  # type: ignore[arg-type]

    flush.assert_awaited_once()


async def test_startup_repair_is_noop_when_history_valid() -> None:
    graph = _FakeGraph([_ai_tool_use("c1"), ToolMessage(content="o", tool_call_id="c1")])
    await _repair_dangling_tool_use_at_startup(graph, agent_id=1)  # type: ignore[arg-type]
    assert graph.updates == []


async def test_startup_repair_is_noop_for_brand_new_agent() -> None:
    graph = _FakeGraph([])
    await _repair_dangling_tool_use_at_startup(graph, agent_id=2)  # type: ignore[arg-type]
    assert graph.updates == []
