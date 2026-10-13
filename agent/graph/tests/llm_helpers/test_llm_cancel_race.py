"""The LLM node discards a partial turn when the cancel event wins its race with the stream."""

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from agent.graph.exec.tests.cancel_fixture import (
    committed_messages,
    has_cancelled_event,
    make_runtime,
    set_after,
)
from agent.graph.llm.node import llm_node
from agent.graph.llm_errors import LlmLedger
from agent.state import AgentState

# Most tests here drive exec_node/llm_node with mocked run_in_subprocess / a
# fake cancel_event, so they are deterministic and run in the parallel pool.
from base.db.code_version_gate import ProcessDbGate
from base.native_process.turn_identity import HostedTurnResources
from tests.fixtures.pin_agent import hosted_resources as hosted_resources

_CONFIG: RunnableConfig = {"configurable": {"thread_id": "7"}}


async def test_llm_node_cancel_event_race_discards_partial(
    hosted_resources: HostedTurnResources,
    fake_cancel_event: asyncio.Event,
    database_gate: ProcessDbGate,
) -> None:
    """cancel_event set during LLM stream → llm_node detects it via asyncio.wait
    race → discards the entire partial generation: Command(halted=True,
    goto=after_exec) commits no messages (generation has no side effects,
    nothing to retain)."""

    async def _stream_then_hang():
        yield AIMessageChunk(content="par")
        yield AIMessageChunk(content="tial code")
        await asyncio.Future()

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _stream_then_hang()
    pub = MagicMock()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    trigger = asyncio.create_task(set_after(fake_cancel_event, 0.1))
    runtime = make_runtime(
        hosted_resources=hosted_resources,
        llm=fake_llm,
        event_publisher=pub,
        database_gate=database_gate,
    )
    result = await llm_node(state, runtime, _CONFIG, ledger=LlmLedger())
    await trigger

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    update = result.update
    assert update is not None
    assert update["halted"] is True
    # Clean discard: no partial AIMessage, no marker
    assert committed_messages(update) == []
    assert has_cancelled_event(pub, agent_id=7)


async def test_llm_node_cancel_event_race_discards_partial_tool_call(
    hosted_resources: HostedTurnResources,
    fake_cancel_event: asyncio.Event,
    database_gate: ProcessDbGate,
) -> None:
    """cancel arrives mid tool_call stream → partial AIMessage is entirely
    discarded, so **no** tool_result debt is incurred: because no complete
    AIMessage with tool_use is committed, history has no "assistant.tool_calls
    must be followed by ToolMessage" protocol debt.

    This is the exact opposite of exec cancel (there tool_use is already
    committed, exec_node must supply a [cancelled by user] tool_result).
    """

    async def _stream_tool_call_then_hang():
        yield AIMessageChunk(content=[{"type": "text", "text": "I'll run that.", "index": 0}])
        yield AIMessageChunk(
            content=[
                {
                    "type": "tool_use",
                    "id": "call_cancelled",
                    "name": "execute_code",
                    "input": {},
                    "index": 1,
                }
            ],
            tool_call_chunks=[
                {"name": "execute_code", "args": "", "id": "call_cancelled", "index": 1},
            ],
        )
        yield AIMessageChunk(
            content=[],
            tool_call_chunks=[
                {"name": None, "args": '{"code": "print(1)', "id": None, "index": 1},
            ],
        )
        await asyncio.Future()

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _stream_tool_call_then_hang()
    pub = MagicMock()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    trigger = asyncio.create_task(set_after(fake_cancel_event, 0.1))
    runtime = make_runtime(
        hosted_resources=hosted_resources,
        llm=fake_llm,
        event_publisher=pub,
        database_gate=database_gate,
    )
    result = await llm_node(state, runtime, _CONFIG, ledger=LlmLedger())
    await trigger

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    update = result.update
    assert update is not None
    assert update["halted"] is True
    # Entirely discarded: neither partial AIMessage nor supplemental tool_result
    assert committed_messages(update) == []
    assert has_cancelled_event(pub, agent_id=7)


async def test_llm_node_cancel_event_race_no_partial_returns_halted(
    hosted_resources: HostedTurnResources,
    fake_cancel_event: asyncio.Event,
    database_gate: ProcessDbGate,
) -> None:
    """cancel_event set before LLM emits anything → llm_node returns Command(halted=True,
    goto=after_exec) without committing any message; does not raise CancelledError
    (which is BaseException and would silently exit the process via asyncio.run)."""

    async def _hang_immediately():
        await asyncio.Future()
        yield  # pragma: no cover

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _hang_immediately()
    pub = MagicMock()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    trigger = asyncio.create_task(set_after(fake_cancel_event, 0.05))
    runtime = make_runtime(
        hosted_resources=hosted_resources,
        llm=fake_llm,
        event_publisher=pub,
        database_gate=database_gate,
    )
    result = await llm_node(state, runtime, _CONFIG, ledger=LlmLedger())
    await trigger
    assert has_cancelled_event(pub, agent_id=7)

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update is not None
    assert result.update["halted"] is True
    assert committed_messages(result.update) == []


async def test_llm_node_cancel_event_race_normal_completion(
    hosted_resources: HostedTurnResources,
    fake_cancel_event: asyncio.Event,
    database_gate: ProcessDbGate,
) -> None:
    """cancel_event not set, LLM stream completes normally → llm_node returns
    Command(goto=before_exec)."""

    async def _fast_stream() -> AsyncIterator[AIMessageChunk]:
        # tool_use must have non-empty tool_calls (validator enforces "protocol consistency"
        # to prevent recurrence of 169)
        yield AIMessageChunk(
            content="print('hi')",
            response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"},
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            tool_call_chunks=[
                {"name": "execute_code", "args": '{"code": "x"}', "id": "call_1", "index": 0},
            ],
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _fast_stream()
    state = AgentState(messages=[HumanMessage(content="go")], halted=False)

    runtime = make_runtime(
        hosted_resources=hosted_resources, llm=fake_llm, database_gate=database_gate
    )
    result = await llm_node(state, runtime, _CONFIG, ledger=LlmLedger())

    assert isinstance(result, Command)
    assert result.update is not None
    assert result.update["messages"][0].content == "print('hi')"  # pyright: ignore[reportUnknownMemberType]
