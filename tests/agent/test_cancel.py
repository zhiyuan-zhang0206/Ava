# pyright: reportOptionalSubscript=false
# Command.update is dict | None; tests always have an update field, narrowing is too verbose
"""Cancel + timeout path tests (exec subprocess model).

Under the cycling topology, cancel goto="after_exec" — after_exec sees halted=True
and routes to claim waiting for the next inbound (cancel does not exit the process,
the agent continues to stand by).

Coverage (cancel/timeout behavior inside nodes):
- `llm_node` cancels the entire partial generation: returns halted Command(goto=after_exec)
  without committing any message — generation has no side effects, and since no complete
  tool_use is committed, no tool_result debt is owed; history remains clean (exec cancel
  is the opposite: already-committed tool_use must be supplemented with a [cancelled by user]
  tool_result by exec_node)
- `llm_node` cancel does not raise CancelledError (that is BaseException, which would
  silently exit the process via asyncio.run)
- `exec_node` cancel stops the turn; timeout returns a marker and continues to the next LLM round
- cancel_event race: tests use a `fake_cancel_event` fixture to monkeypatch
  replace `subscribe_interrupt`, allowing external `event.set()` to trigger cancel —
  not relying on the real DB inbound watcher race (slow + flaky). The production path
  has no test backdoor; signatures are clean.
"""

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent.graph import (
    exec_node,
    llm_node,
)
from agent.graph._context import AvaContext
from agent.graph._exec import (
    _ExecCancelled,
    _ExecCrashed,
    _ExecDone,
    _ExecLifecycle,
    _ExecTimedOut,
)
from agent.state import AgentState
from shared.live_events import EVENT_ADAPTER, Cancelled
from tests.agent._fakes import make_fake_ops_pool

# Most tests here drive exec_node/llm_node with mocked _run_in_subprocess / a
# fake cancel_event, so they are deterministic and run in the parallel pool.


def _has_cancelled_event(pub: MagicMock, agent_id: int) -> bool:
    """assert helper: whether pub.emit calls contain Cancelled(agent_id=...)."""
    for call in pub.emit.call_args_list:
        (payload,) = call.args
        try:
            ev = EVENT_ADAPTER.validate_json(payload)
        except Exception:  # noqa: S112 — skip payloads outside the events union
            continue
        if isinstance(ev, Cancelled) and ev.agent_id == agent_id:
            return True
    return False


def _make_runtime(*, llm=None, ops_pool=None, event_publisher=None) -> Runtime[AvaContext]:
    """Test helper: assemble an AvaContext and wrap it in Runtime."""
    if llm is None:
        llm = MagicMock()
    if isinstance(llm, MagicMock):
        llm.bind_tools.return_value = llm
    if ops_pool is None:
        ops_pool = make_fake_ops_pool()
    ctx = AvaContext(
        ops_pool=ops_pool,  # pyright: ignore[reportUnknownArgumentType]
        llm=llm,  # pyright: ignore[reportUnknownArgumentType]
        event_publisher=event_publisher if event_publisher is not None else MagicMock(),  # pyright: ignore[reportUnknownArgumentType]
    )
    return Runtime(context=ctx)


def _ai_with_code(code: str) -> AIMessage:
    """Single tool wire format: AIMessage with execute_code tool_call."""
    return AIMessage(
        content="",
        tool_calls=[{"name": "execute_code", "args": {"code": code}, "id": "call_test"}],
    )


_CONFIG: RunnableConfig = {"configurable": {"thread_id": "7"}}


async def _set_after(ev: asyncio.Event, delay: float) -> None:
    """Helper: set event after `delay` seconds — used as a cancel_event trigger."""
    await asyncio.sleep(delay)
    ev.set()


# ---------------------------------------------------------------------------
# llm_node cancel_event race paths
# ---------------------------------------------------------------------------


def _committed_messages(update: dict) -> list:
    """The cancel discard path must not commit any message — extract the messages
    from update (missing key or empty list counts as "not committed")."""
    return update.get("messages", [])  # pyright: ignore[reportUnknownMemberType]


async def test_llm_node_cancel_event_race_discards_partial(
    fake_cancel_event: asyncio.Event,
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

    trigger = asyncio.create_task(_set_after(fake_cancel_event, 0.1))
    runtime = _make_runtime(llm=fake_llm, event_publisher=pub)
    result = await llm_node(state, runtime, _CONFIG)
    await trigger

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    update = result.update
    assert update is not None
    assert update["halted"] is True
    # Clean discard: no partial AIMessage, no marker
    assert _committed_messages(update) == []
    assert _has_cancelled_event(pub, agent_id=7)


async def test_llm_node_cancel_event_race_discards_partial_tool_call(
    fake_cancel_event: asyncio.Event,
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

    trigger = asyncio.create_task(_set_after(fake_cancel_event, 0.1))
    runtime = _make_runtime(llm=fake_llm, event_publisher=pub)
    result = await llm_node(state, runtime, _CONFIG)
    await trigger

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    update = result.update
    assert update is not None
    assert update["halted"] is True
    # Entirely discarded: neither partial AIMessage nor supplemental tool_result
    assert _committed_messages(update) == []
    assert _has_cancelled_event(pub, agent_id=7)


async def test_llm_node_cancel_event_race_no_partial_returns_halted(
    fake_cancel_event: asyncio.Event,
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

    trigger = asyncio.create_task(_set_after(fake_cancel_event, 0.05))
    runtime = _make_runtime(llm=fake_llm, event_publisher=pub)
    result = await llm_node(state, runtime, _CONFIG)
    await trigger
    assert _has_cancelled_event(pub, agent_id=7)

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update is not None
    assert result.update["halted"] is True
    assert _committed_messages(result.update) == []


async def test_llm_node_cancel_event_race_normal_completion(
    fake_cancel_event: asyncio.Event,
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

    runtime = _make_runtime(llm=fake_llm)
    result = await llm_node(state, runtime, _CONFIG)

    assert isinstance(result, Command)
    assert result.update["messages"][0].content == "print('hi')"  # pyright: ignore[reportUnknownMemberType]


# ---------------------------------------------------------------------------
# exec_node cancel_event race (mock _run_in_subprocess returns sum-type variant + payload)
# ---------------------------------------------------------------------------


async def test_exec_node_cancel_event_returns_cancelled_command(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel_event triggers → exec_node, via mock, returns cancelled result → Command
    with wrap_code_output cancelled=True + frontend Cancelled event."""

    async def _fake_cancelled(
        code, agent_id, cancel_event, timeout=60.0, chunk_publisher=None, **kwargs
    ):
        return (_ExecCancelled(output="partial work\n"), None)

    monkeypatch.setattr("agent.graph._exec._run_in_subprocess", _fake_cancelled)  # pyright: ignore[reportUnknownArgumentType]

    pub = MagicMock()
    state = AgentState(messages=[_ai_with_code('print("x")')], halted=False)

    runtime = _make_runtime(event_publisher=pub)
    result = await exec_node(state, runtime, _CONFIG)

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    update = result.update
    assert update is not None
    msg = update["messages"][0]
    content = msg.content
    assert "Code execution output" in content
    assert "[cancelled by user]" in content
    assert "partial work" in content
    assert update["halted"] is True
    assert msg.additional_kwargs["ava_cancelled"] is True
    assert msg.additional_kwargs["ava_exit_code"] == -1
    assert _has_cancelled_event(pub, agent_id=7)


async def test_exec_node_cancel_event_race_normal_completion(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel_event not set, the exec completes normally → exec_node returns Command with
    wrap_code_output format ('Code execution output:'), exit_code=0."""

    async def _fake_normal(
        code, agent_id, cancel_event, timeout=60.0, chunk_publisher=None, **kwargs
    ):
        return (_ExecDone(output="hello\n"), None)

    monkeypatch.setattr("agent.graph._exec._run_in_subprocess", _fake_normal)  # pyright: ignore[reportUnknownArgumentType]

    state = AgentState(messages=[_ai_with_code('print("hello")')], halted=False)

    runtime = _make_runtime()
    result = await exec_node(state, runtime, _CONFIG)

    assert isinstance(result, Command)
    msg = result.update["messages"][0]
    content = msg.content  # pyright: ignore[reportUnknownMemberType]
    assert "Code execution output" in content
    assert "hello" in content
    assert "[exit" not in content
    assert msg.additional_kwargs["ava_exit_code"] == 0  # pyright: ignore[reportUnknownMemberType]
    assert msg.additional_kwargs["ava_cancelled"] is False  # pyright: ignore[reportUnknownMemberType]


# ---------------------------------------------------------------------------
# timeout dispatch paths (mock returns the timed-out variant)
# ---------------------------------------------------------------------------


# A timeout-preserves-ava.shell-partial-output test is deliberately absent: the old test
# old `ava.shell.bash` feature that manually forwarded subprocess pipe contents to parent
# stdout on timeout. The new `ava.shell.run` uses stdlib `subprocess.run`, subprocess pipe
# contents are only on `TimeoutExpired.{output,stderr}`, no longer forwarded. Agents wanting
# partial output can use `subprocess.Popen` + read pipes themselves.


async def test_exec_node_timeout_path(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When timeout triggers, exec_node returns a marker but does not halt — the next
    LLM round reads the feedback and changes strategy."""

    async def _fake_timed_out(
        code, agent_id, cancel_event, timeout=60.0, chunk_publisher=None, **kwargs
    ):
        return (_ExecTimedOut(output="partial work\n"), None)

    monkeypatch.setattr("agent.graph._exec._run_in_subprocess", _fake_timed_out)  # pyright: ignore[reportUnknownArgumentType]

    state = AgentState(messages=[_ai_with_code('print("long task")')], halted=False)

    runtime = _make_runtime()
    result = await exec_node(state, runtime, _CONFIG)

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    update = result.update
    assert update is not None
    msg = update["messages"][0]
    content = msg.content
    assert "Code execution output" in content
    assert "[timeout after 60s]" in content
    assert "[cancelled by user]" not in content
    assert update["halted"] is False
    assert msg.additional_kwargs["ava_timed_out"] is True
    assert msg.additional_kwargs["ava_cancelled"] is False
    assert msg.additional_kwargs["ava_exit_code"] == -1


async def test_exec_node_timeout_empty_output(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """timeout triggers and the thread has no output → (no output) marker still appears."""

    async def _fake_empty_timeout(
        code, agent_id, cancel_event, timeout=60.0, chunk_publisher=None, **kwargs
    ):
        return (_ExecTimedOut(output=""), None)

    monkeypatch.setattr("agent.graph._exec._run_in_subprocess", _fake_empty_timeout)  # pyright: ignore[reportUnknownArgumentType]

    state = AgentState(messages=[_ai_with_code('print("x")')], halted=False)

    runtime = _make_runtime()
    result = await exec_node(state, runtime, _CONFIG)

    msg = result.update["messages"][0]
    content = msg.content  # pyright: ignore[reportUnknownMemberType]
    assert "[timeout after 60s]" in content
    assert "(no output)" in content
    assert msg.additional_kwargs["ava_timed_out"] is True  # pyright: ignore[reportUnknownMemberType]


# ---------------------------------------------------------------------------
# StreamingTextIO + ExecOutputChunkPublisher: streaming contract
# ---------------------------------------------------------------------------


def test_streaming_textio_concurrent_writes() -> None:
    """Multiple writer threads writing simultaneously, take_pending pulling concurrently —
    the total content in _buf counts correctly, no characters lost and no interleaving
    breaking single write invocations (the string within a single write remains intact)."""
    import threading as _th

    from agent.graph._exec_stream import StreamingTextIO

    stream = StreamingTextIO()
    n_writers = 4
    writes_per = 200
    payload = "x" * 100  # single write 100 chars

    def writer() -> None:
        for _ in range(writes_per):
            stream.write(payload)

    threads = [_th.Thread(target=writer) for _ in range(n_writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    full = stream.getvalue()
    expected_len = n_writers * writes_per * len(payload)
    assert len(full) == expected_len, f"characters lost: got {len(full)}, want {expected_len}"
    # The 100 'x's within a single write must be contiguous, not sliced by another write
    assert "x" * 100 in full
    # No garbled characters (under lock protection, StringIO write is atomic)
    assert all(c == "x" for c in full)


async def test_streaming_textio_take_pending_increments() -> None:
    """After take_pending pulls once, the next pull only returns the newly added
    content (no duplicates)."""
    from agent.graph._exec_stream import StreamingTextIO

    stream = StreamingTextIO()
    stream.write("aaa")
    assert stream.take_pending() == "aaa"
    assert stream.take_pending() == ""  # all published
    stream.write("bbb")
    stream.write("ccc")
    assert stream.take_pending() == "bbbccc"
    assert stream.getvalue() == "aaabbbccc"  # full content preserved


async def test_chunk_publisher_uses_correct_item_id() -> None:
    """ExecOutputChunkPublisher.publish(text) wraps text into an ExecOutputChunk
    SSE event, with agent_id + item_id matching the constructor; empty text early-
    returns and does not emit. The frontend relies on item_id to append streaming
    chunks to the same code_output item; the final ExecOutput upserts with the same
    id — item_id drift would cause duplicate rendering on the frontend."""
    from agent.graph._exec_stream import ExecOutputChunkPublisher

    emitter = MagicMock()
    pub = ExecOutputChunkPublisher(emitter, agent_id=42, item_id="7.0")

    pub.publish("hello")
    pub.publish("")  # empty text not emitted
    pub.publish(" world")

    # Should have 2 emits (empty text skipped)
    assert emitter.emit.call_count == 2
    # Parse each payload to verify item_id + agent_id
    import json as _json

    for i, expected_text in enumerate(["hello", " world"]):
        (payload,) = emitter.emit.call_args_list[i].args
        ev = _json.loads(payload)
        assert ev["agent_id"] == 42, f"call {i}: agent_id {ev['agent_id']}"
        assert ev["item_id"] == "7.0", f"call {i}: item_id {ev['item_id']}"
        assert ev["content"] == expected_text


# ---------------------------------------------------------------------------
# Halt exception dispatch (exec_node isinstance path coverage)
# ---------------------------------------------------------------------------


async def test_exec_node_dispatch_system_halt(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_ExecLifecycle(_SystemHalt) → halted=True + NO output write-back.

    ava.self.compact commits the compact_summary inbound before raising
    _SystemHalt, and the claim node applies it in the same turn — REMOVE_ALL
    wipes the whole history. Writing the exec output back (ToolMessage +
    ExecOutput SSE) is dead weight: the ToolMessage would be wiped anyway, and
    the SSE event resurfaced as a ghost code_output item in the frontend after
    the compact refresh. So the compact path appends no ToolMessage and emits
    no ExecOutput (only the ExecStart placeholder, which the compact refresh
    clears)."""
    from shared.lifecycle import _SystemHalt

    async def _fake(code, agent_id, cancel_event, timeout=60.0, chunk_publisher=None, **kwargs):
        return (_ExecLifecycle(output="user prep work\n", exc=_SystemHalt()), None)

    emitter = MagicMock()
    monkeypatch.setattr("agent.graph._exec._run_in_subprocess", _fake)  # pyright: ignore[reportUnknownArgumentType]
    state = AgentState(messages=[_ai_with_code('ava.self.compact("s")')], halted=False)
    runtime = _make_runtime(event_publisher=emitter)
    result = await exec_node(state, runtime, _CONFIG)

    assert result.update["halted"] is True
    # No ToolMessage write-back — the compact claim wipes the history right
    # after this turn, so inserting the exec output back would only feed a
    # ghost item to the frontend.
    assert result.update["messages"] == []
    # No ExecOutput SSE write-back either — only the ExecStart placeholder.
    roles = [EVENT_ADAPTER.validate_json(call.args[0]).role for call in emitter.emit.call_args_list]
    assert "exec_start" in roles
    assert "exec_output" not in roles


async def test_exec_node_dispatch_agent_termination(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_ExecLifecycle(AgentTermination) → halted=True + no envelope marker +
    exit_code_for_msg=IDLE_EXIT_CODE (claim side writes the lifecycle marker)."""
    from ava.self import AgentTermination
    from shared.exit_codes import IDLE_EXIT_CODE

    async def _fake(code, agent_id, cancel_event, timeout=60.0, chunk_publisher=None, **kwargs):
        return (_ExecLifecycle(output="", exc=AgentTermination()), None)

    monkeypatch.setattr("agent.graph._exec._run_in_subprocess", _fake)  # pyright: ignore[reportUnknownArgumentType]
    state = AgentState(messages=[_ai_with_code("ava.self.terminate()")], halted=False)
    runtime = _make_runtime()
    result = await exec_node(state, runtime, _CONFIG)

    assert result.update["halted"] is True
    msg = result.update["messages"][0]
    assert "[cancelled by user]" not in msg.content  # pyright: ignore[reportUnknownMemberType]
    assert "[timeout after 60s]" not in msg.content  # pyright: ignore[reportUnknownMemberType]
    assert "[system halt]" not in msg.content  # pyright: ignore[reportUnknownMemberType]
    assert msg.additional_kwargs["ava_exit_code"] == IDLE_EXIT_CODE  # pyright: ignore[reportUnknownMemberType]


async def test_exec_node_dispatch_agent_restart(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_ExecLifecycle(AgentRestart) → halted=True + no envelope marker (symmetric
    with AgentTermination path)."""
    from ava.self import AgentRestart
    from shared.exit_codes import IDLE_EXIT_CODE

    async def _fake(code, agent_id, cancel_event, timeout=60.0, chunk_publisher=None, **kwargs):
        return (_ExecLifecycle(output="", exc=AgentRestart()), None)

    monkeypatch.setattr("agent.graph._exec._run_in_subprocess", _fake)  # pyright: ignore[reportUnknownArgumentType]
    state = AgentState(messages=[_ai_with_code("ava.self.restart()")], halted=False)
    runtime = _make_runtime()
    result = await exec_node(state, runtime, _CONFIG)

    assert result.update["halted"] is True
    msg = result.update["messages"][0]
    assert msg.additional_kwargs["ava_exit_code"] == IDLE_EXIT_CODE  # pyright: ignore[reportUnknownMemberType]


async def test_exec_node_dispatch_ordinary_exception(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records,
) -> None:
    """_ExecCrashed → halted=False + event=exec_failed at INFO (agent
    trial-and-error is not an operator alert; metrics aggregate by event)."""

    async def _fake(code, agent_id, cancel_event, timeout=60.0, chunk_publisher=None, **kwargs):
        return (
            _ExecCrashed(
                output="Traceback...\nValueError: boom\n",
                exc=ValueError("boom"),
            ),
            None,
        )

    monkeypatch.setattr("agent.graph._exec._run_in_subprocess", _fake)  # pyright: ignore[reportUnknownArgumentType]
    state = AgentState(messages=[_ai_with_code('raise ValueError("boom")')], halted=False)
    runtime = _make_runtime()
    result = await exec_node(state, runtime, _CONFIG)

    assert result.update["halted"] is False
    msg = result.update["messages"][0]
    assert msg.additional_kwargs["ava_exit_code"] == 0  # pyright: ignore[reportUnknownMemberType]
    # INFO level — a failed execute_code is ordinary dev feedback, not an
    # operator alert (the event name still feeds per-event metrics).
    failed = [r for r in loguru_records if r["extra"].get("event") == "exec_failed"]  # pyright: ignore[reportUnknownMemberType]
    assert len(failed) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert failed[0]["level"].name == "INFO"  # pyright: ignore[reportUnknownMemberType]
    assert "ValueError" in failed[0]["message"]


async def test_exec_node_dispatch_unknown_lifecycle_subclass_raises(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future new _LifecycleExit subclass that is not handled in the match ladder
    → must fallthrough raise TypeError, not silently land on halted=False (CLAUDE.md
    enumeration dispatch must be exhaustive)."""
    from shared.lifecycle import _LifecycleExit

    class _MysteryLifecycle(_LifecycleExit):
        def __init__(self) -> None:
            super().__init__(0)

    async def _fake(code, agent_id, cancel_event, timeout=60.0, chunk_publisher=None, **kwargs):
        return (_ExecLifecycle(output="", exc=_MysteryLifecycle()), None)

    monkeypatch.setattr("agent.graph._exec._run_in_subprocess", _fake)  # pyright: ignore[reportUnknownArgumentType]
    state = AgentState(messages=[_ai_with_code("...")], halted=False)
    runtime = _make_runtime()

    with pytest.raises(TypeError, match="Unrecognized _LifecycleExit subclass"):
        await exec_node(state, runtime, _CONFIG)
