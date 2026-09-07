# pyright: reportOptionalSubscript=false
# Command.update type dict | None; tests always have update field, narrowing too verbose
"""agent/graph.py streaming behavior tests (async).

mock `BaseChatModel.astream` returns async chunk iterator + AsyncMock redis, validate:
  - llm_node first publishes a code_start
  - each chunk publishes a code_delta (content = chunk.content)
  - llm_node returns AIMessage.content = all chunks concatenated
  - exec_node publishes exec_start at the start
  - exec_node publishes exec_output after finishing

Assert using `EVENT_ADAPTER.validate_json` deserialization——same code path as UI tailer,
if any side field changes, the other side test will turn red."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent.graph import exec_node, llm_node
from agent.graph._context import AvaContext
from agent.state import AgentState
from shared.live_events import EVENT_ADAPTER, ExecOutput, ExecStart
from tests.agent._fakes import make_fake_ops_pool


def _make_runtime(*, llm=None, event_publisher=None) -> Runtime[AvaContext]:
    """test helper: assemble AvaContext into Runtime; ops_pool
    placeholders for nodes that don't actually borrow conns.

    llm_node now `ctx.llm.bind_tools(...)` then astream——set bind_tools
    return llm itself, so fake_llm.astream stub still works after chaining.

    SSE fan-out now goes through `ctx.event_publisher.emit` (non-blocking); default inject a MagicMock
    so node's `assert ctx.event_publisher` passes, tests verifying events pass their own mock in
    to assert `pub.emit.call_args_list`.
    """
    if llm is None:
        llm = MagicMock()
    if isinstance(llm, MagicMock):
        llm.bind_tools.return_value = llm
    ctx = AvaContext(
        ops_pool=make_fake_ops_pool(),
        llm=llm,  # pyright: ignore[reportUnknownArgumentType]
        event_publisher=event_publisher if event_publisher is not None else MagicMock(),  # pyright: ignore[reportUnknownArgumentType]
    )
    return Runtime(context=ctx)


def _ai_with_code(code: str) -> AIMessage:
    """Single tool reconstructed wire format: AIMessage with execute_code tool_call."""
    return AIMessage(
        content="",
        tool_calls=[{"name": "execute_code", "args": {"code": code}, "id": "call_test"}],
    )


async def _aiter(chunks: list[AIMessageChunk]) -> AsyncIterator[AIMessageChunk]:
    """Helper: wrap list into async iterator -- assign fake_llm.astream.return_value
    this generator so `async for` can consume. Each test creates a new generator,
    not reusing across tests (generators are single-use)."""
    for c in chunks:
        yield c


async def test_llm_node_collects_chunks_into_final_message(
    fake_cancel_event: asyncio.Event,
) -> None:
    """llm_node merges streaming chunks into AIMessage into state.messages.

    streaming → Redis behavior is separately tested in tests/agent/test_callbacks.py
    via RedisStreamHandler (agent/graph/_callbacks.py) inline dispatch inside _stream loop;
    this test only verifies node's chunk-to-AIMessage merging semantics.
    """
    fake_llm = MagicMock()
    # Last chunk carries usage_metadata——real provider stream tail has this shape,
    # llm_node internally assert final_msg.usage_metadata must exist (fail-fast guard new generated
    # AIMessage metadata not empty).
    fake_llm.astream.return_value = _aiter(
        [
            AIMessageChunk(content="hello "),
            AIMessageChunk(content="from "),
            AIMessageChunk(
                content="agent",
                response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
        ]
    )
    state = AgentState(messages=[HumanMessage(content="hello")], halted=False)
    config: RunnableConfig = {"configurable": {"thread_id": "7"}}

    result = await llm_node(state, _make_runtime(llm=fake_llm), config)

    assert isinstance(result, Command)
    assert result.update["messages"][0].content == "hello from agent"  # pyright: ignore[reportUnknownMemberType]


def _executed_sql(ops_pool) -> list[str]:
    """SQL strings the shared fake cursor ran during the turn."""
    return [c.args[0] for c in ops_pool.fake_cursor.execute.call_args_list if c.args]  # pyright: ignore[reportUnknownMemberType]


async def test_llm_node_stamps_last_active_at_with_text(
    fake_cancel_event: asyncio.Event,
) -> None:
    """A completed turn that produced text writes both last_active_at (the
    heartbeat idle clock's real-activity anchor) and last_message_text in one
    UPDATE — so a genuine turn resets the agent's idle timer."""
    fake_llm = MagicMock()
    fake_llm.bind_tools.return_value = fake_llm
    fake_llm.astream.return_value = _aiter(
        [
            AIMessageChunk(content="hello "),
            AIMessageChunk(
                content="world",
                response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
        ]
    )
    ops_pool = make_fake_ops_pool()
    ctx = AvaContext(ops_pool=ops_pool, llm=fake_llm, event_publisher=MagicMock())
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)
    await llm_node(state, Runtime(context=ctx), {"configurable": {"thread_id": "7"}})

    stamped = [s for s in _executed_sql(ops_pool) if "last_active_at = now()" in s]
    assert len(stamped) == 1, "a completed turn must issue exactly one last_active_at UPDATE"
    assert "last_message_text" in stamped[0], "a turn with text also persists last_message_text"


async def test_llm_node_stamps_last_active_at_on_tool_only_turn(
    fake_cancel_event: asyncio.Event,
) -> None:
    """A tool-only turn (code, no text) is still real work: it writes
    last_active_at (without last_message_text, since there is no AI text)."""
    fake_llm = MagicMock()
    fake_llm.bind_tools.return_value = fake_llm
    fake_llm.astream.return_value = _aiter(
        [
            AIMessageChunk(content=""),
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {"name": "execute_code", "args": '{"code":"x"}', "id": "call_1", "index": 0},
                ],
            ),
            AIMessageChunk(
                content="",
                response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"},
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
        ]
    )
    ops_pool = make_fake_ops_pool()
    ctx = AvaContext(ops_pool=ops_pool, llm=fake_llm, event_publisher=MagicMock())
    state = AgentState(messages=[HumanMessage(content="run it")], halted=False)
    await llm_node(state, Runtime(context=ctx), {"configurable": {"thread_id": "7"}})

    stamped = [s for s in _executed_sql(ops_pool) if "last_active_at = now()" in s]
    assert len(stamped) == 1, "a tool-only turn must still stamp last_active_at"
    assert "last_message_text" not in stamped[0], "no AI text → no last_message_text in the UPDATE"


async def test_llm_node_dispatches_chunks_to_handler_with_anthropic_shape(
    fake_cancel_event: asyncio.Event,
) -> None:
    """ChatAnthropic + bind_tools real chunk shape (content is list-of-blocks,
    not string) goes through _stream loop → handler.process_chunk → publish full event stream.

    Regression guard: early llm_node went through LangChain `on_llm_new_token` callback,
    after switching to ChatAnthropic callback never fires (`isinstance(content, str)` always
    False, see langchain_anthropic/chat_models.py:1267) — entire stream publish
    path broken. This test uses real anthropic chunk shape to guard inline dispatch path.
    """
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _aiter(
        [
            # message_start: empty content
            AIMessageChunk(content=[]),
            # thinking_delta
            AIMessageChunk(content=[{"type": "thinking", "thinking": "planning", "index": 0}]),
            # text_delta
            AIMessageChunk(content=[{"type": "text", "text": "hi", "index": 1}]),
            # tool_use start: content=[tool_use block] + tool_call_chunks=[args=""]
            AIMessageChunk(
                content=[
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "execute_code",
                        "input": {},
                        "index": 2,
                    }
                ],
                tool_call_chunks=[
                    {"name": "execute_code", "args": "", "id": "call_1", "index": 2},
                ],
            ),
            # input_json_delta: tool_call_chunks carry partial JSON delta
            AIMessageChunk(
                content=[],
                tool_call_chunks=[
                    {"name": None, "args": '{"code":"x"}', "id": None, "index": 2},
                ],
            ),
            # tail message_delta with usage info
            AIMessageChunk(
                content=[],
                response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"},
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
        ]
    )
    pub = MagicMock()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)
    config: RunnableConfig = {"configurable": {"thread_id": "7"}}

    await llm_node(state, _make_runtime(llm=fake_llm, event_publisher=pub), config)

    events = [EVENT_ADAPTER.validate_json(c.args[0]) for c in pub.emit.call_args_list]
    roles = [e.role for e in events]
    # Three independent streams all trigger *Start, finally LLMDone sent by finish()
    assert "reasoning_start" in roles
    assert "reasoning_delta" in roles
    assert "chat_start" in roles
    assert "chat_delta" in roles
    assert "code_start" in roles
    assert "code_delta" in roles
    # node_lifecycle enter publishes timeline_snapshot first (before the LLM
    # stream starts). finish() emits LLMDone when the stream loop completes;
    # TokenUsage is published after final_msg processing, so it lands last.
    assert roles[0] == "timeline_snapshot"
    assert roles[-2:] == ["llm_done", "token_usage"]


async def test_llm_node_publishes_reasoning_tokens(
    fake_cancel_event: asyncio.Event,
) -> None:
    """usage_metadata.output_token_details.reasoning rides through to the
    published TokenUsage event end-to-end (gemini/openai reasoning-token
    surfacing — not just the model/UI layers). Drives the real _llm_node_impl
    publish path with a gemini-shaped final chunk."""
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _aiter(
        [
            AIMessageChunk(content=[{"type": "text", "text": "ok", "index": 0}]),
            AIMessageChunk(
                content=[],
                response_metadata={"model_provider": "google_genai", "finish_reason": "STOP"},
                usage_metadata={
                    "input_tokens": 5,
                    "output_tokens": 200,
                    "total_tokens": 205,
                    "output_token_details": {"reasoning": 180},
                },
            ),
        ]
    )
    pub = MagicMock()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)
    config: RunnableConfig = {"configurable": {"thread_id": "7"}}

    await llm_node(state, _make_runtime(llm=fake_llm, event_publisher=pub), config)

    events = [EVENT_ADAPTER.validate_json(c.args[0]) for c in pub.emit.call_args_list]
    usage = [e for e in events if e.role == "token_usage"]
    assert len(usage) == 1
    assert usage[0].reasoning_tokens == 180
    assert usage[0].output_tokens == 200


async def test_llm_node_preserves_usage_metadata(
    fake_cancel_event: asyncio.Event,
) -> None:
    """usage_metadata must be on the final AIMessage——evals / cost tracking all rely on this
    field. If someday llm_node falls back to only concatenating content without accumulating AIMessageChunk, this
    test will turn red."""
    # Last chunk carries usage_metadata——real Anthropic stream has this
    # shape (usage in message_delta event, lands on last chunk)
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _aiter(
        [
            AIMessageChunk(content="print(1)"),
            AIMessageChunk(
                content="",
                response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
                usage_metadata={
                    "input_tokens": 42,
                    "output_tokens": 7,
                    "total_tokens": 49,
                },
            ),
        ]
    )
    state = AgentState(messages=[HumanMessage(content="go")], halted=False)
    config: RunnableConfig = {"configurable": {"thread_id": "7"}}

    result = await llm_node(state, _make_runtime(llm=fake_llm), config)

    assert isinstance(result, Command)
    msg = result.update["messages"][0]
    assert msg.content == "print(1)"  # pyright: ignore[reportUnknownMemberType]
    assert msg.usage_metadata == {"input_tokens": 42, "output_tokens": 7, "total_tokens": 49}  # pyright: ignore[reportUnknownMemberType]


async def test_exec_node_publishes_exec_start_and_output(
    fake_cancel_event: asyncio.Event,
) -> None:
    """exec_node's two events:
    - before executing publish `exec_start` (UI draws [executing] marker)
    - after finishing publish `exec_output` (UI dev mode sees agent-visible stdout)
    """
    pub = MagicMock()
    state = AgentState(messages=[_ai_with_code('print("ok")')], halted=False)
    config: RunnableConfig = {"configurable": {"thread_id": "7"}}

    # exec_node actually runs subprocess (async) -- use simplest code to avoid side effects
    await exec_node(state, _make_runtime(event_publisher=pub), config)

    events = [EVENT_ADAPTER.validate_json(c.args[0]) for c in pub.emit.call_args_list]
    exec_starts = [e for e in events if isinstance(e, ExecStart)]
    # item_id = f"{exec_msg_idx}.0" where exec_msg_idx = len(state.messages) = 1
    assert exec_starts == [ExecStart(agent_id=7, item_id="1.0")]

    exec_outputs = [e for e in events if isinstance(e, ExecOutput)]
    assert len(exec_outputs) == 1
    assert exec_outputs[0].agent_id == 7
    assert "ok" in exec_outputs[0].content


async def test_exec_node_output_uses_wrap_code_output_envelope(
    fake_cancel_event: asyncio.Event,
) -> None:
    """exec_node after normal completion, appended HumanMessage uses wrap_code_output
    envelope('Code execution output:' prefix), no longer old '[exec output]\\n...'
    format; envelope without [exit N] line(exit code goes through ToolMessage metadata)."""
    # print + flush ensures stdout has content
    state = AgentState(
        messages=[_ai_with_code("import sys; print('envelope_test'); sys.stdout.flush()")],
        halted=False,
    )
    config: RunnableConfig = {"configurable": {"thread_id": "7"}}

    result = await exec_node(state, _make_runtime(), config)

    assert isinstance(result, Command)
    msg = result.update["messages"][0]
    content = msg.content  # pyright: ignore[reportUnknownMemberType]
    # New format
    assert content.startswith("Code execution output"), (  # pyright: ignore[reportUnknownMemberType]
        f"expected wrap_code_output format, got: {content[:60]!r}"
    )
    assert "envelope_test" in content
    # exit code not appearing in envelope text——it goes through ToolMessage metadata
    assert "[exit" not in content
    # But metadata must retain (timeline / hook reading side contract)
    assert msg.additional_kwargs["ava_exit_code"] == 0  # pyright: ignore[reportUnknownMemberType]
    assert msg.additional_kwargs["ava_cancelled"] is False  # pyright: ignore[reportUnknownMemberType]
    # exec wall-clock captured (drives the 'ran in Xs' chip); a real run is
    # non-negative ms — just assert the contract that the field is populated.
    assert isinstance(msg.additional_kwargs["ava_exec_ms"], int)  # pyright: ignore[reportUnknownMemberType]
    assert msg.additional_kwargs["ava_exec_ms"] >= 0  # pyright: ignore[reportUnknownMemberType]
    # Old format should not appear
    assert not content.startswith("[exec output]")  # pyright: ignore[reportUnknownMemberType]


async def test_exec_node_protects_archives_referenced_by_its_current_state(
    fake_cancel_event: asyncio.Event, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real child exec cannot evict the previous output still in native context."""
    from agent.graph import _exec_output
    from shared.config import settings

    directory = tmp_path / ".exec_output"
    monkeypatch.setattr(_exec_output, "_overflow_dir", lambda: directory)
    old_body = ("old payload " * 10 + "\n") * 140
    new_body = ("new payload " * 10 + "\n") * 140
    monkeypatch.setattr(settings.sandbox, "exec_output_crop_archive_max_bytes", len(old_body))
    prior_output = _exec_output.wrap_code_output(old_body)
    archive = next(directory.glob("crop_*.txt"))
    state = AgentState(
        messages=[
            HumanMessage(content=prior_output),
            _ai_with_code("print(('new payload ' * 10 + '\\n') * 140, end='')"),
        ],
        halted=False,
    )
    result = await exec_node(state, _make_runtime(), {"configurable": {"thread_id": "7"}})

    assert new_body in result.update["messages"][0].content  # pyright: ignore[reportUnknownMemberType]
    assert archive.read_text() == old_body
    assert list(directory.glob("crop_*.txt")) == [archive]


# Removed test_exec_node_cancel_output_uses_wrap_code_output_cancelled——it used
# mock _run_in_subprocess synchronously throw SubprocessCancelledPartial simulating cancel.
# After Step 2 cancel-in-node RAII exec_node no longer catches top-level
# SubprocessCancelledPartial (task.cancel() path deleted); real cancel goes through
# cancel_event race → _cancel_subprocess_task internal catch. Coverage path see
# tests/agent/test_cancel.py::test_exec_node_cancel_event_race_captures_partial_stdout
