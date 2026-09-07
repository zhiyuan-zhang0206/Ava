# pyright: reportOptionalSubscript=false
"""mutmut gap-fix unit tests — locks down the actionable cluster in `agent/graph/_llm.py`:

1. `_capture_ava_overview` (3 mutations) — a module-load helper with no dedicated unit test;
   directly import + call, verify that stdout capture actually captures the output of `ava.help(ava)`.
2. Cancel-detection boundary (`_llm_node_impl` mutmut_44) — `cancel_task in done`
   vs `stream first in done` two-path invariant: the cancel branch publishes Cancelled +
   returns halted and does not commit any message (the entire partial generation is discarded);
   the normal branch does not publish Cancelled + calls handler.finish().
3. Additional mutation kills for stop-reason / thinking-block validation.

Baseline source: mutmut llm baseline (PR #302).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import ExecutionInfo, Runtime
from langgraph.types import Command

from agent.graph import llm_node
from agent.graph._context import AvaContext
from agent.graph._llm import (
    _capture_ava_overview,
    _get_ava_overview,
)
from agent.state import AgentState
from shared.live_events import EVENT_ADAPTER, Cancelled
from tests.agent._fakes import make_fake_ops_pool

_CONFIG: RunnableConfig = {"configurable": {"thread_id": "7"}}


def _fixed_task_usage_tally(
    _msg: AIMessage,
    model: str,
    *,
    latency_ms: float | None = None,
    decode_ms: float | None = None,
    priced_at: datetime | None = None,
    task_id: int | None = None,
) -> tuple[int, float]:
    """Stable usage tally for task-metering wiring tests."""
    del model, latency_ms, decode_ms, priced_at, task_id
    return 15, 0.25


# ───────────── _capture_ava_overview ─────────────


def test_capture_ava_overview_returns_non_empty_string() -> None:
    """Direct import + call of `_capture_ava_overview` — lock that `buf = io.StringIO()`
    was not changed to None or list(); `redirect_stdout(buf)` actually captures the
    output of `ava.help(ava)` (mutation `redirect_stdout(None)` would let stdout leak → return ''),
    `buf.getvalue()` is not empty."""
    overview = _capture_ava_overview()

    assert isinstance(overview, str)
    assert len(overview) > 0, "buf did not capture ava.help stdout — redirect_stdout was severed"


def test_capture_ava_overview_surfaces_public_sdk_index() -> None:
    # The overview = `# ava` H1 + one entry per public top-level namespace as
    # `from . import X` + that module's docstring. This is the SDK index, so the
    # agent discovers the gateway primitives (agents / watcher / self) without
    # spelunking; full per-namespace detail stays on demand via ava.help(ava.X).
    # The root docstring was deliberately removed (sysprompt verbosity audit,
    # PR #840) — the `# ava` heading stands alone.
    overview = _capture_ava_overview()

    assert overview.startswith("# ava\n\n"), (
        f"overview does not start with `# ava` heading: {overview[:200]!r}"
    )
    assert "Drill into" not in overview, (
        f"removed root docstring still rendered: {overview[:200]!r}"
    )
    # gateway primitives surfaced as index entries
    for name in ("agents", "watcher", "self", "shell"):
        assert f"from . import {name}" in overview, (
            f"surface primitive {name!r} should appear in overview: {overview[:600]!r}"
        )
    # only `# ava` heading; children render as `from . import` import stubs, not headings
    heading_lines = [
        line
        for line in overview.splitlines()
        if line.lstrip().startswith("#") and not line.lstrip().startswith("#!")
    ]
    assert heading_lines == ["# ava"], (
        f"overview should have only one `# ava` heading, got: {heading_lines!r}"
    )


def test_capture_ava_overview_is_pure_no_stdout_leak(capsys) -> None:
    """`redirect_stdout(buf)` must capture all ava.help output into buf, **not**
    leak to main stdout. Lock that the line `with contextlib.redirect_stdout(buf):`
    was not changed to `with contextlib.redirect_stdout(sys.stdout):` — such a mutation
    would make ava.help actually print to stdout, then capsys.readouterr().out would not be empty."""
    capsys.readouterr()  # clear any prior output  # pyright: ignore[reportUnknownMemberType]
    overview = _capture_ava_overview()
    captured = capsys.readouterr()  # pyright: ignore[reportUnknownMemberType]

    assert overview, "overview is empty"
    assert captured.out == "", (  # pyright: ignore[reportUnknownMemberType]
        f"redirect_stdout ineffective, ava.help content leaked to main stdout: {captured.out[:200]!r}"  # pyright: ignore[reportUnknownMemberType]
    )


def test_get_ava_overview_advertises_registered_plugin_namespace() -> None:
    """A plugin-registered namespace **does** appear in the overview index.

    The namespace itself must be discoverable at the top level even if the
    plugin adds no `register_system_prompt_section` of its own — otherwise a
    top-level namespace would silently vanish and the agent couldn't find it.
    The plugin promotes its *members* in a section; the overview lists the
    *namespace*.
    """
    from types import SimpleNamespace

    import ava

    if "fake_late" in getattr(ava, "_REGISTERED_NAMESPACES", {}):
        ava.clear_registered_namespaces()

    fake_ns = SimpleNamespace(
        __doc__="Fake plugin namespace just for this test.",
        __all_for_ava__=["ping"],
        ping=lambda: "pong",
    )
    fake_ns.ping.__doc__ = "Return pong."

    ava.register_namespace("fake_late", fake_ns)
    try:
        overview = _get_ava_overview()
        assert "fake_late" in overview, (
            f"overview should advertise the registered plugin namespace (otherwise a namespace "
            f"without a section would disappear). overview:\n{overview[:600]}"
        )
    finally:
        ava.clear_registered_namespaces()


# ───────────── cancel-detection boundary (mutmut_44: cancel_task in done) ─────────────
# Baseline doc notes: `if cancel_task in done:` → `if cancel_task not in done:`
# inversion mutation survived. Current `test_llm_node_cancel_event_race_*` series depends
# on `fake_cancel_event` fixture directly `event.set()`, but doesn't assert the exact
# contents of the done set; the inverted in/not in test still passes. Added cases to
# differentiate the two path invariants: "cancel first in done" vs "stream first in done".


def _make_runtime(
    *,
    llm=None,
    event_publisher=None,
    execution_info: ExecutionInfo | None = None,
) -> Runtime[AvaContext]:
    """test helper: assemble Runtime the same way as test_cancel.py.

    llm_node / exec_node don't directly touch ops_pool, so use AsyncMock
    as placeholders. SSE fan-out goes through `ctx.event_publisher.emit`; default to a MagicMock
    so the node's `assert ctx.event_publisher` passes; tests that verify SSE can pass their own
    mock for assertions."""
    if llm is None:
        llm = MagicMock()
    if isinstance(llm, MagicMock):
        llm.bind_tools.return_value = llm
    ctx = AvaContext(
        ops_pool=make_fake_ops_pool(),
        llm=llm,  # pyright: ignore[reportUnknownArgumentType]
        event_publisher=event_publisher if event_publisher is not None else MagicMock(),  # pyright: ignore[reportUnknownArgumentType]
    )
    return Runtime(context=ctx, execution_info=execution_info)


def _has_cancelled_event(pub: MagicMock, agent_id: int) -> bool:
    """assert helper: whether any emit call contains a Cancelled(agent_id=...)."""
    for call in pub.emit.call_args_list:
        (payload,) = call.args
        try:
            ev = EVENT_ADAPTER.validate_json(payload)
        except Exception:  # noqa: S112 — skip non-event union payloads
            continue
        if isinstance(ev, Cancelled) and ev.agent_id == agent_id:
            return True
    return False


async def test_cancel_branch_publishes_cancelled_and_returns_halted(
    fake_cancel_event: asyncio.Event,
) -> None:
    """cancel_event arrives in done set first → llm_node must enter the cancel branch:
    (a) publish a Cancelled event to settings.data_plane.events_channel
    (b) goto=after_exec + halted=True
    (c) the cancel path does **not** call handler.finish() (no LLMDone/TokenUsage publish)

    Locks mutmut_44 `if cancel_task in done:` inversion: after inversion, cancel_task is in
    done but condition is False, falling into stream-normal branch, running stream_task.result()
    triggers CancelledError and raises out the whole node, resulting in no Cancelled event and
    no halted Command — this test verifies both the Cancelled event and the Command shape.
    """

    async def _stream_then_hang() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(content="partial")
        await asyncio.Future()  # hang forever

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _stream_then_hang()
    pub = MagicMock()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    async def _trigger():
        await asyncio.sleep(0.1)
        fake_cancel_event.set()

    trigger = asyncio.create_task(_trigger())
    result = await llm_node(state, _make_runtime(llm=fake_llm, event_publisher=pub), _CONFIG)
    await trigger

    # (a) Cancelled event emitted (cancel branch-exclusive side effect)
    assert _has_cancelled_event(pub, agent_id=7), (
        "cancel branch did not emit Cancelled event —— `cancel_task in done` branch not taken"
    )
    # (b) Command shape: goto=after_exec + halted=True
    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update["halted"] is True
    # (c) cancel path does not call handler.finish() —— no llm_done / token_usage emit
    role_payloads = [c.args[0] for c in pub.emit.call_args_list]
    assert not any('"role":"llm_done"' in p for p in role_payloads), (
        "cancel branch must not emit LLMDone (handler.finish() must not be called)"
    )
    assert not any('"role":"token_usage"' in p for p in role_payloads), (
        "cancel branch must not emit TokenUsage (took normal-completion path)"
    )


async def test_stream_normal_completion_no_cancelled_event(
    fake_cancel_event: asyncio.Event,
) -> None:
    """stream completes first (cancel_event never set) → takes stream-normal branch:
    (a) **does not** publish Cancelled event
    (b) goto=before_exec (tool_calls present → go to exec) or after_exec (idle)
    (c) handler.finish() called → LLMDone event present

    Locks mutmut_44 `if cancel_task in done:` inversion: after inversion the stream-normal
    branch is mistakenly taken as cancel-branch, would publish Cancelled event in a non-cancel
    scenario.
    """

    async def _fast_complete() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="hi",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _fast_complete()
    pub = MagicMock()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    # Note: do not set cancel_event —— stream should complete naturally
    result = await llm_node(state, _make_runtime(llm=fake_llm, event_publisher=pub), _CONFIG)

    # (a) no Cancelled event
    assert not _has_cancelled_event(pub, agent_id=7), (
        "stream-normal branch mistakenly took cancel branch —— `cancel_task in done` mutation flipped"
    )
    # (b) Command shape: end_turn + no tool_calls → halted=True goto=after_exec
    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update["halted"] is True
    # (c) finish() called → LLMDone emitted
    role_payloads = [c.args[0] for c in pub.emit.call_args_list]
    assert any('"role":"llm_done"' in p for p in role_payloads), (
        "stream-normal branch must call handler.finish() and emit LLMDone"
    )


async def test_silent_idle_with_reasoning_continue_loops_not_raises() -> None:
    """No tool_call AND empty text BUT output_tokens > 0 (model produced
    reasoning) → the node no longer raises. It commits the reasoning AIMessage
    and returns halted=False so the claim node loops straight back to the LLM
    (the ava_silent_idle plugin then injects a Continue nudge). No token-wasting
    blind re-stream."""
    from agent.graph._llm import _silent_idle_output_tokens

    _silent_idle_output_tokens.pop("7", None)

    async def _empty_complete() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _empty_complete()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    result = await llm_node(
        state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG
    )

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    # continue-loop: halted=False so claim returns to before_llm without idling
    assert result.update["halted"] is False
    # the reasoning AIMessage is preserved in state.messages
    msgs = result.update["messages"]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], AIMessage)
    _silent_idle_output_tokens.pop("7", None)


async def test_truly_empty_no_reasoning_halts_with_warning(loguru_records) -> None:
    """No tool_call AND empty text AND output_tokens=0 (model truly produced
    nothing, not even reasoning) → the existing WARNING + halt path still
    applies — retrying a deterministic empty output wastes API credits."""

    async def _empty_complete() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _empty_complete()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    result = await llm_node(
        state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG
    )

    assert isinstance(result, Command)
    assert result.update["halted"] is True
    silent = [
        r
        for r in loguru_records
        if "EMPTY text" in r["message"] and r["level"].name == "WARNING"  # pyright: ignore[reportUnknownMemberType]
    ]
    assert len(silent) == 1, "truly empty (no tokens) must log exactly one distinct WARNING"  # pyright: ignore[reportUnknownArgumentType]


# ───────────── extra actionable mutation kill ─────────────


def test_validate_stop_reason_unexpected_carries_stop_reason_and_output_tokens() -> None:
    """`LLMStreamUnexpectedStopReasonError` raise must carry stop_reason and
    output_tokens attributes (programmatic dispatch does not rely on regex parsing the message).
    mutmut: `stop_reason=stop_reason` → `stop_reason=None` /
    `output_tokens=output_tokens` → `output_tokens=None`.

    Existing `test_validate_raises_on_pause_turn` only matches the message and does not read
    attributes; `test_validate_raises_on_max_tokens` reads attributes but only covers the
    Truncated subclass path — the unexpected parent class path is uncovered.
    """
    from agent.graph._llm import (
        LLMStreamUnexpectedStopReasonError,
        _validate_stop_reason,
    )

    msg = AIMessage(
        content="",
        response_metadata={"model_provider": "anthropic", "stop_reason": "pause_turn"},
        usage_metadata={"input_tokens": 10, "output_tokens": 99, "total_tokens": 109},
    )
    with pytest.raises(LLMStreamUnexpectedStopReasonError) as exc_info:
        _validate_stop_reason(msg)
    assert exc_info.value.stop_reason == "pause_turn", (
        f"stop_reason attribute must be actually set (got {exc_info.value.stop_reason!r}) —— "
        "mutation `stop_reason=None` makes this assertion red"
    )
    assert exc_info.value.output_tokens == 99, (
        f"output_tokens attribute must be actually set (got {exc_info.value.output_tokens!r}) —— "
        "mutation `output_tokens=None` makes this assertion red"
    )


def test_text_display_uses_dot_text_for_list_content() -> None:
    """AIMessage.text on list content extracts plain text, not a stringified list.

    Locks the langchain-normalized `.text` behavior the `_llm_node_impl` display
    line relies on: a Gemini-style list-of-blocks response yields clean text, not
    `str([{"type": "text", "text": "..."}])`.
    """
    msg = AIMessage(content=[{"type": "text", "text": "Two plus two equals four."}])
    assert msg.text == "Two plus two equals four."
    assert not msg.text.startswith("[")  # not str(list)


async def test_cancel_event_set_before_first_chunk_returns_halted_no_publish_done(
    fake_cancel_event: asyncio.Event,
) -> None:
    """cancel_event set immediately (before first chunk) → cancel branch: the entire
    generation is discarded, Command(halted=True, goto=after_exec) does not commit any
    message, and handler.finish() is not called (no LLMDone)."""

    async def _hang_forever() -> AsyncIterator[AIMessageChunk]:
        await asyncio.Future()
        yield  # type: ignore[unreachable]  # pragma: no cover

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _hang_forever()
    pub = MagicMock()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    # set immediately — stream has not yet yielded a first chunk
    fake_cancel_event.set()

    result = await llm_node(state, _make_runtime(llm=fake_llm, event_publisher=pub), _CONFIG)

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update is not None
    assert result.update["halted"] is True
    # clean discard: no message committed
    assert result.update.get("messages", []) == []
    # Cancelled event emitted
    assert _has_cancelled_event(pub, agent_id=7)
    # handler.finish() not called → no LLMDone
    role_payloads = [c.args[0] for c in pub.emit.call_args_list]
    assert not any('"role":"llm_done"' in p for p in role_payloads)


# ───────────── Silent idle supplementary tests (PR #35 review, agent #976) ─────────────


async def test_silent_idle_with_thinking_blocks_continue_loops() -> None:
    """thinking blocks present but output_tokens=0 → still judged as silent idle.

    The first condition of `has_reasoning`: when content contains a type="thinking" block,
    even if output_tokens=0 it should be recognized as "produced reasoning but no action" →
    continue-loop, rather than falling into the truly-empty WARNING + halt path.

    Locks the silent_idle thinking-block condition so it is not coupled with the output_tokens > 0 condition.
    """
    from agent.graph._llm import _silent_idle_output_tokens

    _silent_idle_output_tokens.pop("7", None)

    async def _thinking_only_chunk() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content=[{"type": "thinking", "thinking": "Let me reason...", "signature": "sig-x"}],
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 10, "output_tokens": 0, "total_tokens": 10},
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _thinking_only_chunk()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    result = await llm_node(
        state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG
    )

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update["halted"] is False
    msgs = result.update["messages"]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], AIMessage)
    _silent_idle_output_tokens.pop("7", None)


def test_record_consecutive_error_tracks_and_clears() -> None:
    """`_record_consecutive_error` correctly accumulates same-type errors; `_clear_consecutive_errors` resets.

    Directly operate on the `_consecutive_errors` dict, verifying:
    - first record → count=1
    - same type recorded again → count=2
    - after reset the entry disappears
    """
    from agent.graph._llm import (
        LLMStreamSilentIdleError,
        _clear_consecutive_errors,
        _consecutive_errors,
        _record_consecutive_error,
    )

    tid = "test-thread-1"
    _consecutive_errors.pop(tid, None)  # clean up leftovers

    exc = LLMStreamSilentIdleError("test", output_tokens=1)
    _record_consecutive_error(tid, exc)
    assert _consecutive_errors[tid] == ("LLMStreamSilentIdleError", 1), (
        "first record should be count=1"
    )

    _record_consecutive_error(tid, exc)
    assert _consecutive_errors[tid] == ("LLMStreamSilentIdleError", 2), (
        "same type recorded again should be count=2"
    )

    _clear_consecutive_errors(tid)
    assert tid not in _consecutive_errors, "entry should disappear after reset"


def test_check_consecutive_error_cap_raises_fatal_on_exhaustion() -> None:
    """When cap is exhausted, `_check_consecutive_error_cap` raises FatalLLMStreamError.

    Pre-fill _consecutive_errors to the cap value (default 3),
    `_check_consecutive_error_cap` should raise FatalLLMStreamError and pop the entry.
    """
    from agent.graph._llm import (
        FatalLLMStreamError,
        _check_consecutive_error_cap,
        _consecutive_errors,
    )

    tid = "test-thread-2"
    _consecutive_errors[tid] = ("LLMStreamSilentIdleError", 3)

    with pytest.raises(FatalLLMStreamError, match="retry cap"):
        _check_consecutive_error_cap(tid)

    # After cap exhaustion the entry is popped, next turn restarts counting
    assert tid not in _consecutive_errors, (
        "_check_consecutive_error_cap must pop entry after exhaustion"
    )


def test_check_consecutive_error_cap_below_threshold_passes() -> None:
    """Below cap, `_check_consecutive_error_cap` returns normally without raising."""
    from agent.graph._llm import (
        _check_consecutive_error_cap,
        _consecutive_errors,
    )

    tid = "test-thread-3"
    _consecutive_errors[tid] = ("LLMStreamSilentIdleError", 2)  # < cap(3)

    # should not raise
    _check_consecutive_error_cap(tid)

    assert _consecutive_errors[tid] == ("LLMStreamSilentIdleError", 2), (
        "below cap must not alter entry"
    )
    _consecutive_errors.pop(tid, None)  # clean up


async def test_silent_idle_with_deepseek_reasoning_content_continue_loops() -> None:
    """DeepSeek model's reasoning is in `additional_kwargs.reasoning_content`,
    not in Anthropic's content blocks thinking type — silent_idle detection
    must also cover this path, otherwise DeepSeek reasoning-only turn would be missed.

    Construct: output_tokens=0, no text, no tool_call, no thinking blocks,
    but has `additional_kwargs.reasoning_content` → judged silent idle → continue-loop.
    """
    from agent.graph._llm import _silent_idle_output_tokens

    _silent_idle_output_tokens.pop("7", None)

    async def _ds_reasoning_only() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 5, "output_tokens": 0, "total_tokens": 5},
            additional_kwargs={"reasoning_content": "Let me analyze the request step by step..."},
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _ds_reasoning_only()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    result = await llm_node(
        state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG
    )

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update["halted"] is False
    msgs = result.update["messages"]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], AIMessage)
    _silent_idle_output_tokens.pop("7", None)


async def test_silent_idle_zero_output_reasoning_content_consumes_minimum_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reasoning-content-only turns cannot bypass the silent-idle cost guard."""
    from agent.graph._llm import _silent_idle_output_tokens
    from shared.config import settings

    _silent_idle_output_tokens.pop("7", None)
    monkeypatch.setattr(settings.lm, "llm_silent_idle_max_output_tokens", 3)

    async def _reasoning_content_only() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 5, "output_tokens": 0, "total_tokens": 5},
            additional_kwargs={"reasoning_content": "I still need to think."},
        )

    for turn in range(1, 4):
        fake_llm = MagicMock()
        fake_llm.astream.return_value = _reasoning_content_only()
        result = await llm_node(
            AgentState(messages=[HumanMessage(content="hi")], halted=False),
            _make_runtime(llm=fake_llm, event_publisher=MagicMock()),
            _CONFIG,
        )
        assert isinstance(result, Command)
        assert result.update["halted"] is (turn == 3)

    assert "7" not in _silent_idle_output_tokens


# ─────────── provider-error taxonomy: fail-fast (permanent) vs retry (transient) ───────────
# The status→ErrorClass mapping itself is covered exhaustively in
# tests/agent/test_provider_errors.py (classify_error). These drive the wiring
# through llm_node: a PERMANENT class becomes a fail-fast FatalProviderError, a
# TRANSIENT class re-raises for the RetryPolicy.


class _FakeProviderStatusError(Exception):
    """anthropic/openai APIStatusError shape used to drive the llm_node classifier."""

    def __init__(self, status_code: int, body: dict | None = None) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.body = body  # pyright: ignore[reportUnknownMemberType]


def _astream_raising(exc: Exception) -> AsyncIterator[AIMessageChunk]:
    async def _gen() -> AsyncIterator[AIMessageChunk]:
        raise exc
        yield  # pragma: no cover — unreachable; only marks this an async generator

    return _gen()


async def test_llm_node_permanent_provider_error_fails_fast_with_structured_fields(
    loguru_records,
) -> None:
    """A PERMANENT provider error (HTTP 400 — bad request / context length /
    schema) raised mid-stream becomes a FatalProviderError carrying the
    classifier's structured (error_class, provider, status), and the structured
    `llm_provider_error` log lands error_class=permanent / status=400 / fatal=True.
    The RetryPolicy excludes FatalProviderError, so the agent idles instead of
    burning the ~16-min backoff budget and dying."""
    from agent.graph._llm import FatalProviderError, _consecutive_errors
    from shared.config import settings

    _consecutive_errors.pop("7", None)
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _astream_raising(_FakeProviderStatusError(400))
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    with pytest.raises(FatalProviderError) as exc_info:
        await llm_node(state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG)

    assert exc_info.value.error_class == "permanent"
    assert exc_info.value.status == 400
    classify_logs = [r for r in loguru_records if r["extra"].get("event") == "llm_provider_error"]  # pyright: ignore[reportUnknownMemberType]
    assert len(classify_logs) == 1, "exactly one structured classification log per failed call"  # pyright: ignore[reportUnknownArgumentType]
    assert classify_logs[0]["extra"]["error_class"] == "permanent"
    assert classify_logs[0]["extra"]["status"] == 400
    assert classify_logs[0]["extra"]["fatal"] is True
    # Every classification log carries the billing verdict and the model that
    # failed, not only the billing ones — the alert filters on billing=true, so
    # an ordinary failure must state False rather than omit the key.
    assert classify_logs[0]["extra"]["billing"] is False
    assert classify_logs[0]["extra"]["model"] == settings.lm.llm_model


async def test_llm_node_billing_error_logs_billing_vendor_and_model(loguru_records) -> None:
    """A 402 (DeepSeek's `Insufficient Balance`) lands billing=True plus the
    vendor + model on the `llm_provider_error` log — the three fields the
    ava-ops-llm-billing-quota rule filters and groups on, and the ones its IM
    message interpolates. Without them an out-of-credit key is indistinguishable
    from any other permanent rejection in the event stream."""
    from agent.graph._llm import FatalProviderError, _consecutive_errors
    from shared.config import settings

    _consecutive_errors.pop("7", None)
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _astream_raising(_FakeProviderStatusError(402))
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    original = settings.lm.llm_model
    try:
        settings.lm.llm_model = "deepseek-v4-flash"
        with pytest.raises(FatalProviderError) as exc_info:
            await llm_node(state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG)
    finally:
        settings.lm.llm_model = original

    assert "out of credit or quota" in str(exc_info.value)
    classify_logs = [r for r in loguru_records if r["extra"].get("event") == "llm_provider_error"]  # pyright: ignore[reportUnknownMemberType]
    assert len(classify_logs) == 1  # pyright: ignore[reportUnknownArgumentType]
    extra = classify_logs[0]["extra"]
    assert extra["billing"] is True
    assert extra["status"] == 402
    assert extra["vendor"] == "deepseek"
    assert extra["model"] == "deepseek-v4-flash"


async def test_llm_node_transient_provider_error_propagates_for_retry() -> None:
    """A TRANSIENT provider error (HTTP 500) is re-raised as-is — NOT wrapped in
    FatalProviderError — so the LangGraph RetryPolicy retries it. Fail-fast is
    reserved for permanent classes; a transient blip must keep retrying."""
    from agent.graph._llm import FatalProviderError, _consecutive_errors

    _consecutive_errors.pop("7", None)
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _astream_raising(_FakeProviderStatusError(500))
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    with pytest.raises(_FakeProviderStatusError) as exc_info:
        await llm_node(state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG)
    assert not isinstance(exc_info.value, FatalProviderError)


async def test_llm_node_configured_fatal_error_type_fails_fast() -> None:
    """A configured fatal error *type* (e.g. engine_overloaded_error) surfacing on
    a transient-nature status (429) still fails fast: retrying an overloaded engine
    in-turn is futile, so it becomes a FatalProviderError (error_class records the
    transient nature; fatal=True records the fail-fast action)."""
    from agent.graph._llm import FatalProviderError, _consecutive_errors
    from shared.config import settings

    _consecutive_errors.pop("7", None)
    original = settings.lm.llm_fatal_provider_error_types
    try:
        settings.lm.llm_fatal_provider_error_types = "engine_overloaded_error"
        exc = _FakeProviderStatusError(
            429, {"error": {"type": "engine_overloaded_error", "message": "overloaded"}}
        )
        fake_llm = MagicMock()
        fake_llm.astream.return_value = _astream_raising(exc)
        # A configured-fatal error type triggers _consume_llm's one non-streaming
        # fallback; make ainvoke fail the same way so the error survives to the
        # classify block instead of the fallback masking it.
        fake_llm.ainvoke = AsyncMock(side_effect=exc)
        state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

        with pytest.raises(FatalProviderError) as exc_info:
            await llm_node(state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG)
        assert exc_info.value.error_class == "transient"
        assert exc_info.value.status == 429
    finally:
        settings.lm.llm_fatal_provider_error_types = original


async def test_silent_idle_guard_halts_at_cumulative_output_token_cap(loguru_records) -> None:
    """Silent-idle output consumes one token budget and reports its cost."""
    from agent.graph._llm import _silent_idle_output_tokens
    from shared.config import settings

    _silent_idle_output_tokens.pop("7", None)
    cap = settings.lm.llm_silent_idle_max_output_tokens
    assert cap == 2048

    async def _reasoning_only() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 1_100, "total_tokens": 1_101},
        )

    for turn in range(1, 3):
        fake_llm = MagicMock()
        fake_llm.astream.return_value = _reasoning_only()
        state = AgentState(messages=[HumanMessage(content="hi")], halted=False)
        result = await llm_node(
            state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG
        )
        assert isinstance(result, Command)
        assert result.goto == "after_exec"
        if turn == 1:
            assert result.update["halted"] is False
        else:
            assert result.update["halted"] is True

    # The budget is popped at the cap, so the next run starts fresh.
    assert "7" not in _silent_idle_output_tokens
    silent_logs = [
        r
        for r in loguru_records
        if r["extra"].get("event") == "silent_idle"  # pyright: ignore[reportUnknownMemberType]
    ]
    assert silent_logs[-1]["extra"]["cumulative_output_tokens"] == 2_200
    assert silent_logs[-1]["extra"]["estimated_cost_usd"] > 0


async def test_retried_llm_node_records_total_retry_duration(loguru_records) -> None:
    """A success after retry exports the full sequence duration as telemetry."""

    async def _text_turn() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="done",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _text_turn()
    runtime = _make_runtime(
        llm=fake_llm,
        event_publisher=MagicMock(),
        execution_info=ExecutionInfo(
            checkpoint_id="checkpoint",
            checkpoint_ns="",
            task_id="task",
            node_attempt=2,
            node_first_attempt_time=time.time() - 3.0,
        ),
    )

    await llm_node(
        AgentState(messages=[HumanMessage(content="hi")], halted=False), runtime, _CONFIG
    )

    retry_logs = [
        record
        for record in loguru_records
        if record["extra"].get("event") == "llm_retry"  # pyright: ignore[reportUnknownMemberType]
    ]
    assert retry_logs[-1]["extra"]["outcome"] == "succeeded"
    assert retry_logs[-1]["extra"]["duration_seconds"] >= 3.0


async def test_silent_idle_streak_resets_after_normal_turn() -> None:
    """A real action clears the silent-idle output-token budget."""
    from agent.graph._llm import _silent_idle_output_tokens

    _silent_idle_output_tokens.pop("7", None)

    async def _reasoning_only() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    async def _text_turn() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="here is my answer",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 4, "total_tokens": 5},
        )

    # 1) silent idle accumulates its output-token cost.
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _reasoning_only()
    await llm_node(
        AgentState(messages=[HumanMessage(content="hi")], halted=False),
        _make_runtime(llm=fake_llm, event_publisher=MagicMock()),
        _CONFIG,
    )
    assert _silent_idle_output_tokens.get("7") == 1

    # 2) a normal text turn resets the streak
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _text_turn()
    result = await llm_node(
        AgentState(messages=[HumanMessage(content="hi")], halted=False),
        _make_runtime(llm=fake_llm, event_publisher=MagicMock()),
        _CONFIG,
    )
    assert result.update["halted"] is True  # text, no tool_call → halt
    assert "7" not in _silent_idle_output_tokens

    # 3) a later silent idle starts back at one output token, not two.
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _reasoning_only()
    result = await llm_node(
        AgentState(messages=[HumanMessage(content="hi")], halted=False),
        _make_runtime(llm=fake_llm, event_publisher=MagicMock()),
        _CONFIG,
    )
    assert result.update["halted"] is False
    assert _silent_idle_output_tokens.get("7") == 1
    _silent_idle_output_tokens.pop("7", None)


# ────────────────────────────────────────────────────────────
# _parse_provider_error_type / _is_fatal_provider_error_type
# ────────────────────────────────────────────────────────────


class _FakeOpenAIError(Exception):
    """Simulates openai.RateLimitError / openai.APIStatusError shape."""

    def __init__(self, body: dict | None = None) -> None:
        super().__init__("fake error")
        self.body = body  # pyright: ignore[reportUnknownMemberType]


class _FakeAnthropicError(Exception):
    """Simulates anthropic.RateLimitError / anthropic.APIStatusError shape."""

    def __init__(self, body: dict | None = None) -> None:
        super().__init__("fake error")
        self.body = body  # pyright: ignore[reportUnknownMemberType]


def test_parse_provider_error_type_openai_shape() -> None:
    """OpenAI SDK errors carry body.error.type — extract it."""
    from agent.graph._llm import _parse_provider_error_type

    exc = _FakeOpenAIError(
        {
            "error": {
                "type": "engine_overloaded_error",
                "message": "The engine is currently overloaded",
            }
        }
    )
    assert _parse_provider_error_type(exc) == "engine_overloaded_error"


def test_parse_provider_error_type_anthropic_shape() -> None:
    """Anthropic SDK errors use the same body.error.type shape."""
    from agent.graph._llm import _parse_provider_error_type

    exc = _FakeAnthropicError(
        {
            "error": {
                "type": "overloaded_error",
                "message": "Overloaded",
            }
        }
    )
    assert _parse_provider_error_type(exc) == "overloaded_error"


def test_parse_provider_error_type_no_body() -> None:
    """Exception without a body attribute returns None."""
    from agent.graph._llm import _parse_provider_error_type

    assert _parse_provider_error_type(ConnectionError("net")) is None


def test_parse_provider_error_type_body_none() -> None:
    """Exception with body=None returns None."""
    from agent.graph._llm import _parse_provider_error_type

    exc = _FakeOpenAIError(None)
    assert _parse_provider_error_type(exc) is None


def test_parse_provider_error_type_body_not_dict() -> None:
    """Exception with body as a non-dict (string, list) returns None."""
    from agent.graph._llm import _parse_provider_error_type

    exc = _FakeOpenAIError("not a dict")  # type: ignore[arg-type]
    assert _parse_provider_error_type(exc) is None


def test_parse_provider_error_type_no_error_key() -> None:
    """body without 'error' key returns None."""
    from agent.graph._llm import _parse_provider_error_type

    exc = _FakeOpenAIError({"status": "error"})
    assert _parse_provider_error_type(exc) is None


def test_parse_provider_error_type_error_not_dict() -> None:
    """body.error not a dict returns None."""
    from agent.graph._llm import _parse_provider_error_type

    exc = _FakeOpenAIError({"error": "server_error"})
    assert _parse_provider_error_type(exc) is None


def test_parse_provider_error_type_no_type_key() -> None:
    """body.error without 'type' key returns None."""
    from agent.graph._llm import _parse_provider_error_type

    exc = _FakeOpenAIError({"error": {"message": "oops"}})
    assert _parse_provider_error_type(exc) is None


def test_parse_provider_error_type_empty_string() -> None:
    """body.error.type is an empty string — returns None (not a meaningful type)."""
    from agent.graph._llm import _parse_provider_error_type

    exc = _FakeOpenAIError({"error": {"type": ""}})
    assert _parse_provider_error_type(exc) is None


def test_is_fatal_provider_error_type_matches_configured() -> None:
    """When the error type is in the configured fatal set, returns True."""
    from agent.graph._llm import _is_fatal_provider_error_type
    from shared.config import settings

    original = settings.lm.llm_fatal_provider_error_types
    try:
        settings.lm.llm_fatal_provider_error_types = "engine_overloaded_error"
        exc = _FakeOpenAIError(
            {"error": {"type": "engine_overloaded_error", "message": "overloaded"}}
        )
        assert _is_fatal_provider_error_type(exc) is True
    finally:
        settings.lm.llm_fatal_provider_error_types = original


def test_is_fatal_provider_error_type_not_in_set() -> None:
    """Error type not in the configured set returns False."""
    from agent.graph._llm import _is_fatal_provider_error_type
    from shared.config import settings

    original = settings.lm.llm_fatal_provider_error_types
    try:
        settings.lm.llm_fatal_provider_error_types = "engine_overloaded_error"
        exc = _FakeOpenAIError({"error": {"type": "rate_limit_exceeded", "message": "slow down"}})
        assert _is_fatal_provider_error_type(exc) is False
    finally:
        settings.lm.llm_fatal_provider_error_types = original


def test_is_fatal_provider_error_type_empty_config() -> None:
    """Empty configured set is a fast no-op (always returns False)."""
    from agent.graph._llm import _is_fatal_provider_error_type
    from shared.config import settings

    original = settings.lm.llm_fatal_provider_error_types
    try:
        settings.lm.llm_fatal_provider_error_types = ""
        exc = _FakeOpenAIError(
            {"error": {"type": "engine_overloaded_error", "message": "overloaded"}}
        )
        assert _is_fatal_provider_error_type(exc) is False
    finally:
        settings.lm.llm_fatal_provider_error_types = original


def test_is_fatal_provider_error_type_no_body() -> None:
    """Exception without body (generic exception) returns False."""
    from agent.graph._llm import _is_fatal_provider_error_type

    assert _is_fatal_provider_error_type(ConnectionError("net")) is False


async def test_llm_usage_event_carries_latency_ms(loguru_records) -> None:
    """The whole-call wall-clock lands on the llm_usage agent_event.

    `_stream_with_cache_retry` stamps `handler.llm_latency_ms` after the call
    completes, and `_finalize_turn_observability` forwards it to
    `log_llm_usage(latency_ms=...)` — the ops monitor panel's latency/TPS
    source. A real stream (one chunk with usage_metadata) must produce an
    llm_usage record with a positive latency_ms in its payload extras.
    """
    from agent.graph._llm import _consecutive_errors

    _consecutive_errors.pop("7", None)

    async def _one_chunk() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="hi",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _one_chunk()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)
    await llm_node(state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG)

    usage = [r for r in loguru_records if r["extra"].get("event") == "llm_usage"]  # pyright: ignore[reportUnknownMemberType]
    assert len(usage) == 1, "exactly one llm_usage record per completed call"  # pyright: ignore[reportUnknownArgumentType]
    lat = usage[0]["extra"]["latency_ms"]
    assert lat is not None and lat > 0, f"latency_ms should be a positive ms float, got {lat!r}"
    assert usage[0]["extra"]["model"] == "deepseek-v4-flash-vision-exp"


async def test_completed_task_turn_records_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task-tagged completed turn forwards its measured usage to that task only."""
    import agent.graph._llm as llm_module
    from ava_builtins.plugins.ava_fleet import task_registry

    recorded: list[tuple[int, int, float]] = []

    async def _one_chunk() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="hi",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    def record(task_id: int, *, token_count: int, cost_usd: float) -> None:
        recorded.append((task_id, token_count, cost_usd))

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _one_chunk()
    monkeypatch.setattr(llm_module, "log_llm_usage", _fixed_task_usage_tally)
    monkeypatch.setattr(task_registry, "record_task_usage", record)

    result = await llm_node(
        AgentState(messages=[HumanMessage(content="hi")], halted=False, active_task_id=42),
        _make_runtime(llm=fake_llm, event_publisher=MagicMock()),
        _CONFIG,
    )

    assert result.goto == "after_exec"
    assert recorded == [(42, 15, 0.25)]


async def test_task_usage_failure_does_not_break_completed_turn(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    """A metering-store outage cannot turn one completed LLM call into a retry."""
    import agent.graph._llm as llm_module
    from ava_builtins.plugins.ava_fleet import task_registry

    async def _one_chunk() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="hi",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    def unavailable(_task_id: int, *, token_count: int, cost_usd: float) -> None:
        del token_count, cost_usd
        raise OSError("task database unavailable")

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _one_chunk()
    monkeypatch.setattr(llm_module, "log_llm_usage", _fixed_task_usage_tally)
    monkeypatch.setattr(task_registry, "record_task_usage", unavailable)

    result = await llm_node(
        AgentState(messages=[HumanMessage(content="hi")], halted=False, active_task_id=42),
        _make_runtime(llm=fake_llm, event_publisher=MagicMock()),
        _CONFIG,
    )

    assert result.goto == "after_exec"
    assert fake_llm.astream.call_count == 1
    assert any(
        record["extra"].get("label") == "task-usage"
        and record["extra"].get("body") == "failed to record usage for task 42"
        for record in loguru_records
    )
