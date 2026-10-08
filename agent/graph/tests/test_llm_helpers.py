# pyright: reportOptionalSubscript=false
"""mutmut gap-fix unit tests — locks down the actionable cluster in `agent/graph/llm/node.py`:

1. `_capture_ava_overview` (3 mutations, now in `agent/graph/prompt/_base_prompt.py`) — a module-load
   helper with no dedicated unit test; directly import + call, verify that stdout capture
   actually captures the output of `ava.help(ava)`.
2. Cancel-detection boundary (`_llm_node_impl` mutmut_44) — `cancel_task in done`
   vs `stream first in done` two-path invariant: the cancel branch publishes Cancelled +
   returns halted and does not commit any message (the entire partial generation is discarded);
   the normal branch does not publish Cancelled + calls handler.finish().
3. Additional mutation kills for stop-reason / thinking-block validation.

Baseline source: mutmut llm baseline (PR #302).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import ExecutionInfo, Runtime
from langgraph.types import Command

from agent.graph import llm_node
from agent.graph.llm_errors import LlmLedger
from agent.graph.prompt._base_prompt import _capture_ava_overview, _get_ava_overview
from agent.state import AgentState
from agent.tests._fakes import make_fake_ops_pool
from base.agents.context import AvaContext
from base.db import Database
from base.events.live.bus import EventBus
from base.events.live.projection import EVENT_ADAPTER, Cancelled
from base.host.env.agent_slices import AgentSlices, LlmCallPolicy

_CONFIG: RunnableConfig = {"configurable": {"thread_id": "7"}}


@pytest.fixture
def ledger() -> LlmLedger:
    """A fresh ledger per test: the node's error counts and silent-idle budget start empty."""
    return LlmLedger()


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


def test_get_ava_overview_advertises_installed_plugin_namespace() -> None:
    """A plugin-installed namespace **does** appear in the overview index.

    The namespace itself must be discoverable at the top level even if the
    plugin adds no declared system prompt section of its own — otherwise a
    top-level namespace would silently vanish and the agent couldn't find it.
    The plugin promotes its *members* in a section; the overview lists the
    *namespace*.
    """
    from types import SimpleNamespace

    from ava.sdk_surface import plugins

    fake_ns = SimpleNamespace(
        __doc__="Fake plugin namespace just for this test.",
        __all_for_ava__=["ping"],
        ping=lambda: "pong",
    )
    fake_ns.ping.__doc__ = "Return pong."

    undo = plugins.install_namespace("fake-plugin", "fake_late", fake_ns, {})
    try:
        overview = _get_ava_overview()
        assert "fake_late" in overview, (
            f"overview should advertise the installed plugin namespace (otherwise a namespace "
            f"without a section would disappear). overview:\n{overview[:600]}"
        )
    finally:
        undo()


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
        agent=AgentSlices.resolve(),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
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
    ledger: LlmLedger,
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
    result = await llm_node(
        state, _make_runtime(llm=fake_llm, event_publisher=pub), _CONFIG, ledger=ledger
    )
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
    ledger: LlmLedger,
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
    result = await llm_node(
        state, _make_runtime(llm=fake_llm, event_publisher=pub), _CONFIG, ledger=ledger
    )

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


async def test_silent_idle_with_reasoning_continue_loops_not_raises(ledger: LlmLedger) -> None:
    """No tool_call AND empty text BUT output_tokens > 0 (model produced
    reasoning) → the node no longer raises. It commits the reasoning AIMessage
    and returns halted=False so the claim node loops straight back to the LLM
    (the ava_silent_idle plugin then injects a Continue nudge). No token-wasting
    blind re-stream."""

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
        state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG, ledger=ledger
    )

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    # continue-loop: halted=False so claim returns to before_llm without idling
    assert result.update["halted"] is False
    # the reasoning AIMessage is preserved in state.messages
    msgs = result.update["messages"]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], AIMessage)


async def test_truly_empty_no_reasoning_halts_with_warning(
    loguru_records, ledger: LlmLedger
) -> None:
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
        state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG, ledger=ledger
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
    from agent.graph.llm._chunk import _validate_stop_reason
    from agent.graph.llm_errors import LLMStreamUnexpectedStopReasonError

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
    ledger: LlmLedger,
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

    result = await llm_node(
        state, _make_runtime(llm=fake_llm, event_publisher=pub), _CONFIG, ledger=ledger
    )

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


async def test_silent_idle_with_thinking_blocks_continue_loops(ledger: LlmLedger) -> None:
    """thinking blocks present but output_tokens=0 → still judged as silent idle.

    The first condition of `has_reasoning`: when content contains a type="thinking" block,
    even if output_tokens=0 it should be recognized as "produced reasoning but no action" →
    continue-loop, rather than falling into the truly-empty WARNING + halt path.

    Locks the silent_idle thinking-block condition so it is not coupled with the output_tokens > 0 condition.
    """

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
        state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG, ledger=ledger
    )

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update["halted"] is False
    msgs = result.update["messages"]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], AIMessage)


def test_record_consecutive_error_tracks_and_clears(ledger: LlmLedger) -> None:
    """`LlmLedger.record_consecutive_error` accumulates same-type errors; `clear_consecutive_errors` resets.

    Operate on the ledger's consecutive-error record, verifying:
    - first record → count=1
    - same type recorded again → count=2
    - after reset the entry disappears
    """
    from agent.graph.llm_errors import LLMStreamSilentIdleError

    tid = "test-thread-1"

    exc = LLMStreamSilentIdleError("test", output_tokens=1)
    ledger.record_consecutive_error(tid, exc)
    assert ledger.consecutive_error(tid) == ("LLMStreamSilentIdleError", 1), (
        "first record should be count=1"
    )

    ledger.record_consecutive_error(tid, exc)
    assert ledger.consecutive_error(tid) == ("LLMStreamSilentIdleError", 2), (
        "same type recorded again should be count=2"
    )

    ledger.clear_consecutive_errors(tid)
    assert ledger.consecutive_error(tid) is None, "entry should disappear after reset"


def test_check_consecutive_error_cap_raises_fatal_on_exhaustion(ledger: LlmLedger) -> None:
    """When cap is exhausted, `check_consecutive_error_cap` raises FatalLLMStreamError.

    Pre-fill the ledger's record to the cap value (default 3),
    `check_consecutive_error_cap` should raise FatalLLMStreamError and pop the entry.
    """
    from agent.graph.llm_errors import FatalLLMStreamError, LLMStreamSilentIdleError

    tid = "test-thread-2"
    for _ in range(3):
        ledger.record_consecutive_error(tid, LLMStreamSilentIdleError("test", output_tokens=1))

    with pytest.raises(FatalLLMStreamError, match="retry cap"):
        ledger.check_consecutive_error_cap(tid)

    # After cap exhaustion the entry is popped, next turn restarts counting
    assert ledger.consecutive_error(tid) is None, (
        "check_consecutive_error_cap must pop entry after exhaustion"
    )


def test_check_consecutive_error_cap_below_threshold_passes(ledger: LlmLedger) -> None:
    """Below cap, `check_consecutive_error_cap` returns normally without raising."""
    from agent.graph.llm_errors import LLMStreamSilentIdleError

    tid = "test-thread-3"
    for _ in range(2):  # < cap(3)
        ledger.record_consecutive_error(tid, LLMStreamSilentIdleError("test", output_tokens=1))

    # should not raise
    ledger.check_consecutive_error_cap(tid)

    assert ledger.consecutive_error(tid) == ("LLMStreamSilentIdleError", 2), (
        "below cap must not alter entry"
    )


async def test_silent_idle_with_deepseek_reasoning_content_continue_loops(
    ledger: LlmLedger,
) -> None:
    """DeepSeek model's reasoning is in `additional_kwargs.reasoning_content`,
    not in Anthropic's content blocks thinking type — silent_idle detection
    must also cover this path, otherwise DeepSeek reasoning-only turn would be missed.

    Construct: output_tokens=0, no text, no tool_call, no thinking blocks,
    but has `additional_kwargs.reasoning_content` → judged silent idle → continue-loop.
    """

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
        state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG, ledger=ledger
    )

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update["halted"] is False
    msgs = result.update["messages"]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], AIMessage)


async def test_silent_idle_zero_output_reasoning_content_consumes_minimum_budget(
    monkeypatch: pytest.MonkeyPatch,
    ledger: LlmLedger,
) -> None:
    """Reasoning-content-only turns cannot bypass the silent-idle cost guard."""
    from base.config import settings

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
            ledger=ledger,
        )
        assert isinstance(result, Command)
        assert result.update["halted"] is (turn == 3)

    assert ledger.silent_idle_output_tokens("7") == 0


# ─────────── provider-error taxonomy: fail-fast (permanent) vs retry (transient) ───────────
# The status→ErrorClass mapping itself is covered exhaustively in
# base/lm/tests/providers/test_provider_errors.py (classify_error). These drive the wiring
# through llm_node: a PERMANENT class becomes a fail-fast FatalProviderError, a
# TRANSIENT class re-raises for the retry loop.


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
    ledger: LlmLedger,
) -> None:
    """A PERMANENT provider error (HTTP 400 — bad request / context length /
    schema) raised mid-stream becomes a FatalProviderError carrying the
    classifier's structured (error_class, provider, status), and the structured
    `llm_provider_error` log lands error_class=permanent / status=400 / fatal=True.
    The retry loop excludes FatalProviderError, so the agent idles instead of
    burning the ~16-min backoff budget and dying."""
    from agent.graph.llm_errors import FatalProviderError
    from base.config import settings

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _astream_raising(_FakeProviderStatusError(400))
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    with pytest.raises(FatalProviderError) as exc_info:
        await llm_node(
            state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG, ledger=ledger
        )

    assert exc_info.value.error_class == "permanent"
    assert exc_info.value.status == 400
    classify_logs = [r for r in loguru_records if r["extra"].get("event") == "llm_provider_error"]  # pyright: ignore[reportUnknownMemberType]
    assert len(classify_logs) == 1, "exactly one structured classification log per failed call"  # pyright: ignore[reportUnknownArgumentType]
    assert classify_logs[0]["extra"]["error_class"] == "permanent"
    assert classify_logs[0]["extra"]["status"] == 400
    assert classify_logs[0]["extra"]["fatal"] is True
    # Every provider failure records an explicit billing verdict.
    assert classify_logs[0]["extra"]["billing"] is False
    assert classify_logs[0]["extra"]["model"] == settings.lm.llm_model


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


def _fatal(error_type: str | None, configured: str) -> bool:
    """`_is_fatal_provider_error_type` for an error of `error_type` under a configured fatal set."""
    from agent.graph.llm_errors import _is_fatal_provider_error_type

    body = {"error": {"type": error_type, "message": "m"}}
    policy = LlmCallPolicy(
        configured, gemini_explicit_cache_enabled=False, gemini_cache_timeout_seconds=1.0
    )
    return _is_fatal_provider_error_type(_FakeOpenAIError(body), policy)


__all__ = ["_FakeAnthropicError"]
