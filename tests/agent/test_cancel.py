# pyright: reportOptionalSubscript=false
# Command.update is dict | None; tests always have an update field, narrowing is too verbose
"""Cancel + timeout path tests (in-process thread model).

Under the cycling topology, cancel goto="after_exec" — after_exec sees halted=True
and routes to claim waiting for the next inbound (cancel does not exit the process,
the agent continues to stand by).

Coverage (cancel/timeout behavior inside nodes):
- When `_run_in_thread` receives cancel_event, it uses ctypes to async-raise
  KeyboardInterrupt to interrupt the worker thread; partial output (including
  traceback) is preserved in result.output
- `_run_in_thread` 60s timeout uses ctypes to async-raise TimeoutError to interrupt;
  both pure Python infinite loops and blocking syscalls respond; the fallback for
  native code stuck (orphaned + daemon=True) is not tested here
- Cancel has priority over timeout race
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
import sys
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent.graph import (
    _run_in_thread,
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
from agent.graph._exec_capture import current_capture
from agent.state import AgentState
from shared.live_events import EVENT_ADAPTER, Cancelled
from tests.agent._fakes import make_fake_ops_pool

# Most tests here drive exec_node/llm_node with mocked _run_in_thread / a fake
# cancel_event, so they are deterministic and run in the parallel pool. Only the
# tests that spawn a *real* worker thread and rely on real wall-clock
# timeout/cancel timing keep `@pytest.mark.flaky` to run serial.


@pytest.fixture
def fast_join_grace(monkeypatch: pytest.MonkeyPatch):
    """Shrink the orphaned-thread join grace for real-thread tests so they don't
    each pay the full production 2.0s. A worker stuck in a blocking `time.sleep`
    C syscall never responds to the ctypes async-exc within grace — it always
    orphans — so a shorter grace only declares the orphan sooner without changing
    any outcome. Partial output is captured before the sleep, independent of
    grace. (Mirrors the inline pattern already used by
    test_orphaned_exec_thread_leaves_process_streams_usable.)"""
    import agent.graph._exec as _exec_mod

    monkeypatch.setattr(_exec_mod, "_THREAD_JOIN_GRACE_S", 0.25)


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
# _run_in_thread direct paths (cancel_event trigger, ctypes async raise)
# ---------------------------------------------------------------------------


@pytest.mark.flaky  # real worker thread + ctypes cancel injection
async def test_run_in_thread_cancel_event_stops_thread(fast_join_grace) -> None:
    """`_run_in_thread` receives cancel_event then uses ctypes to inject
    KeyboardInterrupt to interrupt the worker thread, returns _ExecCancelled
    variant, and overall elapsed time is reasonable.

    `time.sleep(60)` is a blocking syscall, but CPython `time.sleep` periodically
    returns to the interpreter to check signals, responding to ctypes injection
    within hundreds of milliseconds."""
    cancel_event = asyncio.Event()
    trigger = asyncio.create_task(_set_after(cancel_event, 0.3))

    t0 = time.monotonic()
    result = await _run_in_thread(
        code="import time; time.sleep(60)",
        agent_id="1",
        cancel_event=cancel_event,
        timeout=10.0,
        chunk_publisher=None,
    )
    elapsed = time.monotonic() - t0
    await trigger

    assert isinstance(result, _ExecCancelled), (
        f"Expected _ExecCancelled, got {type(result).__name__}"
    )
    # cancel 0.3s + ctypes injection response + thread join grace ≤ 5s is generous
    assert elapsed < 5.0, f"cancel overall time {elapsed:.2f}s too long"


@pytest.mark.flaky  # real worker thread + ctypes cancel injection
async def test_run_in_thread_cancel_preserves_partial_output(fast_join_grace) -> None:
    """stdout/stderr already printed by the worker thread is preserved in
    result.output after cancel.

    `print` / `sys.stderr.write` are both redirected to the same StreamingTextIO;
    after cancel injects KeyboardInterrupt, the thread, in the except BaseException
    handler, also writes the agent-facing (filtered) traceback to the same stream
    → all in result.output."""
    code = (
        "import sys, time\n"
        "print('hello', flush=True)\n"
        "print('world', flush=True)\n"
        "sys.stderr.write('warn!\\n'); sys.stderr.flush()\n"
        "time.sleep(60)\n"
    )
    cancel_event = asyncio.Event()
    # Leave 0.5s for the thread to write 3 lines to the stream, then set cancel_event
    trigger = asyncio.create_task(_set_after(cancel_event, 0.5))

    result = await _run_in_thread(
        code=code,
        agent_id="1",
        cancel_event=cancel_event,
        timeout=10.0,
        chunk_publisher=None,
    )
    await trigger

    assert isinstance(result, _ExecCancelled)
    output = result.output
    assert "hello" in output and "world" in output, f"partial stdout lost: {output!r}"
    assert "warn" in output, f"stderr (merged) lost: {output!r}"


async def test_orphaned_exec_thread_leaves_process_streams_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop while the worker is stuck in code that swallows the injected
    interrupt orphans the thread before its capture binding unwinds. The
    binding is context-scoped (`agent/graph/_exec_capture.py`), so the
    process's own sys.stdout/sys.stderr keep resolving to the real streams —
    the next node's faulthandler stall dump still finds a usable fd. Before
    the per-context capture landed this was a process-global assignment, and
    an orphan left sys.stderr pointed at the fileno-less capture buffer for
    the rest of the process."""
    import agent.graph._exec as _exec_mod

    monkeypatch.setattr(_exec_mod, "_THREAD_JOIN_GRACE_S", 0.1)

    cancel_event = asyncio.Event()
    trigger = asyncio.create_task(_set_after(cancel_event, 0.1))
    # Agent code that catches the injected KeyboardInterrupt and keeps running →
    # the worker never reaches its redirect's __exit__ → orphaned past the grace.
    code = (
        "import time\n"
        "while True:\n"
        "    try:\n"
        "        time.sleep(100)\n"
        "    except BaseException:\n"
        "        continue\n"
    )
    result = await _run_in_thread(
        code=code,
        agent_id="1",
        cancel_event=cancel_event,
        timeout=10.0,
        chunk_publisher=None,
    )
    await trigger

    assert isinstance(result, _ExecCancelled)
    # The orphan is still running and still holds its capture in its own
    # context. From here — the framework's context — both streams must resolve
    # to something with a real fd, which the capture buffer does not have.
    sys.stdout.fileno()
    sys.stderr.fileno()
    assert current_capture() is None, "orphaned thread's capture leaked into the caller"


async def test_timeout_orphan_is_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A thread that survives the deadline injection is killed by the bounded
    reaper instead of leaking until process exit (Task #1058): the one-shot
    TimeoutError is invisible to a thread inside `time.sleep`, and code that
    swallows `Exception` survives it forever (TimeoutError IS an Exception).
    The reaper re-injects KeyboardInterrupt on a cadence — a BaseException, so
    `except Exception` swallow loops cannot survive it — and the thread dies
    within the cadence of its native call returning."""
    import agent.graph._exec as _exec_mod

    monkeypatch.setattr(_exec_mod, "_THREAD_JOIN_GRACE_S", 0.1)
    import agent.graph._exec_threads as _exec_threads_mod

    monkeypatch.setattr(_exec_threads_mod, "_THREAD_REAP_INTERVAL_S", 0.05)
    monkeypatch.setattr(_exec_threads_mod, "_THREAD_REAP_WINDOW_S", 5.0)

    cancel_event = asyncio.Event()
    # The exact #1058 orphan class: a sleep loop that swallows Exception.
    code = (
        "import time\n"
        "while True:\n"
        "    try:\n"
        "        time.sleep(0.01)\n"
        "    except Exception:\n"
        "        pass\n"
    )
    result = await _run_in_thread(
        code=code,
        agent_id="reap-test",
        cancel_event=cancel_event,
        timeout=0.2,
        chunk_publisher=None,
    )
    assert isinstance(result, _ExecTimedOut)

    workers = [t for t in threading.enumerate() if t.name == "exec-reap-test"]
    assert len(workers) == 1, f"expected one orphaned worker thread, got {len(workers)}"
    t = workers[0]
    deadline = time.monotonic() + 5.0
    while t.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not t.is_alive(), "the reaper failed to kill the orphaned exec thread"


# ---------------------------------------------------------------------------
# llm_node cancel_event race paths (unrelated to thread changes, ported)
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
# exec_node cancel_event race (mock _exec_with_cancel_event returns sum-type variant)
# ---------------------------------------------------------------------------


async def test_exec_node_cancel_event_kills_thread(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel_event triggers → exec_node, via mock, returns cancelled result → Command
    with wrap_code_output cancelled=True + frontend Cancelled event."""

    async def _fake_cancelled(code, agent_id, cancel_event, timeout=60.0, **kwargs):
        return _ExecCancelled(output="partial work\n")

    monkeypatch.setattr("agent.graph._exec._exec_with_cancel_event", _fake_cancelled)  # pyright: ignore[reportUnknownArgumentType]

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
    """cancel_event not set, thread completes normally → exec_node returns Command with
    wrap_code_output format ('Code execution output:'), exit_code=0."""

    async def _fake_normal(code, agent_id, cancel_event, timeout=60.0, **kwargs):
        return _ExecDone(output="hello\n")

    monkeypatch.setattr("agent.graph._exec._exec_with_cancel_event", _fake_normal)  # pyright: ignore[reportUnknownArgumentType]

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
# timeout paths (60s default, tests use shorter timeout)
# ---------------------------------------------------------------------------


@pytest.mark.flaky  # real worker thread + ctypes timeout injection
async def test_run_in_thread_timeout_pure_python_loop() -> None:
    """Pure Python infinite loop `while True: x += 1` —— every bytecode is an
    async exc check boundary, ctypes injected TimeoutError responds immediately."""
    cancel_event = asyncio.Event()
    t0 = time.monotonic()
    result = await _run_in_thread(
        code="x = 0\nwhile True: x += 1\n",
        agent_id="9999",
        cancel_event=cancel_event,
        timeout=0.5,
        chunk_publisher=None,
    )
    elapsed = time.monotonic() - t0

    assert isinstance(result, _ExecTimedOut), f"Expected _ExecTimedOut, got {type(result).__name__}"
    # deadline = start + timeout, so injection can only fire at/after the timeout,
    # never before — the lower bound proves the timeout actually waited (didn't
    # short-circuit / return instantly) rather than pinning a specific duration.
    assert 0.3 < elapsed < 5.0, f"Expected ~0.5s elapsed, got {elapsed:.2f}s"


@pytest.mark.flaky  # real worker thread stuck in a blocking syscall → timeout orphan
async def test_run_in_thread_timeout_blocking_sleep(fast_join_grace) -> None:
    """`time.sleep(30)` blocking syscall —— thread stuck in C layer does not respond
    to ctypes injection, goes through join-grace expiry orphan path and returns timeout
    result."""
    cancel_event = asyncio.Event()
    t0 = time.monotonic()
    result = await _run_in_thread(
        code="import time; time.sleep(30)",
        agent_id="9999",
        cancel_event=cancel_event,
        timeout=0.5,
        chunk_publisher=None,
    )
    elapsed = time.monotonic() - t0

    assert isinstance(result, _ExecTimedOut)
    # elapsed ≈ timeout + join grace; lower bound proves the timeout waited (see
    # the pure-python variant), upper bound proves it didn't run the full sleep(30).
    assert 0.3 < elapsed < 5.0, f"Expected ~0.75s elapsed, got {elapsed:.2f}s"


@pytest.mark.flaky  # real worker thread: cancel-vs-timeout race
async def test_run_in_thread_cancel_priority_over_timeout(fast_join_grace) -> None:
    """cancel_event set before timeout → _ExecCancelled (cancel always wins)."""
    cancel_event = asyncio.Event()
    trigger = asyncio.create_task(_set_after(cancel_event, 0.5))

    t0 = time.monotonic()
    result = await _run_in_thread(
        code="import time; time.sleep(30)",
        agent_id="9998",
        cancel_event=cancel_event,
        timeout=3.0,
        chunk_publisher=None,
    )
    elapsed = time.monotonic() - t0
    await trigger

    assert isinstance(result, _ExecCancelled), (
        f"cancel should have priority, got {type(result).__name__}"
    )
    assert elapsed < 3.0, f"cancel should have been ~0.5s, got {elapsed:.2f}s"


@pytest.mark.flaky  # real worker thread + ctypes timeout injection
async def test_run_in_thread_timeout_preserves_partial_output(fast_join_grace) -> None:
    """When timeout triggers, content already printed by the thread is preserved in
    result.output."""
    cancel_event = asyncio.Event()
    result = await _run_in_thread(
        code=(
            "import sys, time\n"
            "print('before sleep', flush=True)\n"
            "sys.stderr.write('stderr line\\n'); sys.stderr.flush()\n"
            "time.sleep(30)\n"
            "print('after sleep', flush=True)\n"
        ),
        agent_id="9997",
        cancel_event=cancel_event,
        timeout=0.5,
        chunk_publisher=None,
    )

    assert isinstance(result, _ExecTimedOut)
    output = result.output
    assert "before sleep" in output, f"stdout should be preserved: {output!r}"
    assert "stderr line" in output, f"stderr (merged) should be preserved: {output!r}"
    assert "after sleep" not in output, "print after sleep should not appear"


# `test_run_in_thread_timeout_preserves_ava_shell_partial_output` removed: it tested the
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

    async def _fake_timed_out(code, agent_id, cancel_event, timeout=60.0, **kwargs):
        return _ExecTimedOut(output="partial work\n")

    monkeypatch.setattr("agent.graph._exec._exec_with_cancel_event", _fake_timed_out)  # pyright: ignore[reportUnknownArgumentType]

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

    async def _fake_empty_timeout(code, agent_id, cancel_event, timeout=60.0, **kwargs):
        return _ExecTimedOut(output="")

    monkeypatch.setattr("agent.graph._exec._exec_with_cancel_event", _fake_empty_timeout)  # pyright: ignore[reportUnknownArgumentType]

    state = AgentState(messages=[_ai_with_code('print("x")')], halted=False)

    runtime = _make_runtime()
    result = await exec_node(state, runtime, _CONFIG)

    msg = result.update["messages"][0]
    content = msg.content  # pyright: ignore[reportUnknownMemberType]
    assert "[timeout after 60s]" in content
    assert "(no output)" in content
    assert msg.additional_kwargs["ava_timed_out"] is True  # pyright: ignore[reportUnknownMemberType]


# ---------------------------------------------------------------------------
# in-process self-modification: sys.modules cache survives disk writes
# ---------------------------------------------------------------------------


async def test_in_process_sys_modules_cache_survives_disk_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the main process already imported a module, even if a worker thread
    corrupts the source file on disk, subsequent `import <module>` inside exec
    still hits the sys.modules cache — it does not read the disk.

    Uses tmp_path to create a temporary module + monkeypatch to inject sys.path
    and sys.modules, avoiding modification of the real ava/shell.py (so that even
    if pytest SIGINT / abnormal exit leaves no cleanup, no bad files remain).
    """
    import importlib

    # 1. Create a valid module under tmp_path
    mod_dir = tmp_path / "_smoke_pkg"
    mod_dir.mkdir()
    (mod_dir / "__init__.py").write_text("")
    mod_file = mod_dir / "victim.py"
    mod_file.write_text("def alive():\n    return True\n")

    monkeypatch.syspath_prepend(str(tmp_path))  # pyright: ignore[reportUnknownMemberType]
    # cleanup uses monkeypatch.delitem to auto-undo
    monkeypatch.setitem(sys.modules, "_smoke_pkg", importlib.import_module("_smoke_pkg"))
    monkeypatch.setitem(
        sys.modules, "_smoke_pkg.victim", importlib.import_module("_smoke_pkg.victim")
    )

    # 2. Write a syntax error to the disk version
    mod_file.write_text("this is not valid python <<<\n")

    # 3. The sys.modules cache still exists → exec's import hits the cache → no error
    cancel_event = asyncio.Event()
    result = await _run_in_thread(
        code=("import _smoke_pkg.victim as v\nprint('cache hit:', v.alive())\n"),
        agent_id="1",
        cancel_event=cancel_event,
        timeout=5.0,
        chunk_publisher=None,
    )
    assert isinstance(result, _ExecDone), f"Expected _ExecDone, got {type(result).__name__}"
    assert "cache hit: True" in result.output, f"output: {result.output!r}"


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

    async def _fake(code, agent_id, cancel_event, timeout=60.0, **kwargs):
        return _ExecLifecycle(output="user prep work\n", exc=_SystemHalt())

    emitter = MagicMock()
    monkeypatch.setattr("agent.graph._exec._exec_with_cancel_event", _fake)  # pyright: ignore[reportUnknownArgumentType]
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

    async def _fake(code, agent_id, cancel_event, timeout=60.0, **kwargs):
        return _ExecLifecycle(output="", exc=AgentTermination())

    monkeypatch.setattr("agent.graph._exec._exec_with_cancel_event", _fake)  # pyright: ignore[reportUnknownArgumentType]
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

    async def _fake(code, agent_id, cancel_event, timeout=60.0, **kwargs):
        return _ExecLifecycle(output="", exc=AgentRestart())

    monkeypatch.setattr("agent.graph._exec._exec_with_cancel_event", _fake)  # pyright: ignore[reportUnknownArgumentType]
    state = AgentState(messages=[_ai_with_code("ava.self.restart()")], halted=False)
    runtime = _make_runtime()
    result = await exec_node(state, runtime, _CONFIG)

    assert result.update["halted"] is True
    msg = result.update["messages"][0]
    assert msg.additional_kwargs["ava_exit_code"] == IDLE_EXIT_CODE  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.flaky  # real worker thread + ctypes cancel injection
async def test_run_in_thread_lifecycle_priority_over_cancel() -> None:
    """lifecycle has priority over cancel: when the worker thread has stored a
    `_LifecycleExit` (AgentTermination) and the cancel_event is also set,
    `_run_in_thread` must construct _ExecLifecycle, not _ExecCancelled — moving
    "lifecycle always wins" into the constructor (mis-inserting an [cancelled by user]
    envelope marker is now impossible at the type level). Locks the construction order
    mutation (check cancelled before lifecycle).

    Construction requires exc=_LifecycleExit **and** cancelled=True to both hold true.
    Previously we used "immediately raise AgentTermination + synchronously set cancel"
    to create this state, but that was a real GIL race: the injected KeyboardInterrupt
    often landed on the `raise AgentTermination()` line → exc became KeyboardInterrupt
    instead of _LifecycleExit (hit rate ~1/3 when coverage tracing slows the worker,
    and CI serial with coverage would fail). Changed to a deterministic approach: the
    worker runs in a loop, turning the injected cancel interrupt into AgentTermination —
    no matter when the injection lands, it's caught by the except and elevated to
    lifecycle, so exc=_LifecycleExit and cancelled=True are always simultaneously true,
    no longer depending on scheduling timing. Cancel delay 0.15s ensures the injection
    lands inside the loop (not during the import phase).
    """
    from ava.self import AgentTermination

    cancel_event = asyncio.Event()
    # cancel arrives after the worker enters the loop; the worker's interrupt handler
    # elevates to lifecycle.
    trigger = asyncio.create_task(_set_after(cancel_event, 0.15))
    code = (
        "from ava.self import AgentTermination\n"
        "while True:\n"
        "    try:\n"
        "        for _ in range(100_000_000):\n"
        "            pass\n"
        "    except BaseException:\n"
        "        raise AgentTermination()\n"
    )

    result = await _run_in_thread(
        code=code,
        agent_id="1",
        cancel_event=cancel_event,
        timeout=5.0,
        chunk_publisher=None,
    )
    await trigger

    assert isinstance(result, _ExecLifecycle), (
        f"lifecycle must have priority over cancel, got {type(result).__name__}"
    )
    assert isinstance(result.exc, AgentTermination)


async def test_exec_node_dispatch_ordinary_exception(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records,
) -> None:
    """_ExecCrashed → halted=False + event=exec_failed at INFO (agent
    trial-and-error is not an operator alert; metrics aggregate by event)."""

    async def _fake(code, agent_id, cancel_event, timeout=60.0, **kwargs):
        return _ExecCrashed(
            output="Traceback...\nValueError: boom\n",
            exc=ValueError("boom"),
        )

    monkeypatch.setattr("agent.graph._exec._exec_with_cancel_event", _fake)  # pyright: ignore[reportUnknownArgumentType]
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

    async def _fake(code, agent_id, cancel_event, timeout=60.0, **kwargs):
        return _ExecLifecycle(output="", exc=_MysteryLifecycle())

    monkeypatch.setattr("agent.graph._exec._exec_with_cancel_event", _fake)  # pyright: ignore[reportUnknownArgumentType]
    state = AgentState(messages=[_ai_with_code("...")], halted=False)
    runtime = _make_runtime()

    with pytest.raises(TypeError, match="Unrecognized _LifecycleExit subclass"):
        await exec_node(state, runtime, _CONFIG)


# ---------------------------------------------------------------------------
# E2E in-thread lifecycle: real ava.self.* SDK call → _ExecLifecycle variant
# replaces the historical "agent 141 manual smoke test", automating the entire chain
# from worker thread → ava import → SDK lifecycle exception → _ExecLifecycle construction.
# ---------------------------------------------------------------------------


async def test_run_in_thread_e2e_ava_self_terminate(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: agent code `import ava; ava.self.terminate()` runs in worker
    thread → SDK INSERT terminate inbound (mocked) → raises AgentTermination →
    _run_in_thread catches and constructs _ExecLifecycle(exc=AgentTermination).

    This is the most critical invariant of the in-process refactor: the lifecycle
    SDK call written from the agent's perspective must be able to run completely
    inside the worker thread, crossing sys.modules + ctypes-eligible Python
    runtime. Under the subprocess model this used the subprocess exit code 42
    channel; after moving in-process, it uses exception isinstance dispatch, and
    the end-to-end posture had no automated test without manual smoke testing."""
    from ava.self import AgentTermination

    # ava.self.terminate() is a single unconditional INSERT of a terminate
    # inbound, then raises AgentTermination — no delivery-obligation SELECT gate.
    # Mock the cursor so the test does not depend on a live Postgres.
    fake_cursor = MagicMock()
    fake_cursor.execute.return_value = None
    fake_cursor_cm = MagicMock()
    fake_cursor_cm.__enter__ = MagicMock(return_value=fake_cursor)
    fake_cursor_cm.__exit__ = MagicMock(return_value=None)
    fake_db = MagicMock()
    fake_db.cursor.return_value = fake_cursor_cm
    monkeypatch.setattr("ava.DB", fake_db)

    cancel_event = asyncio.Event()
    result = await _run_in_thread(
        code="import ava\nava.self.terminate()",
        agent_id="1",
        cancel_event=cancel_event,
        timeout=5.0,
        chunk_publisher=None,
    )

    assert isinstance(result, _ExecLifecycle), (
        f"Expected _ExecLifecycle, got {type(result).__name__} (result={result})"
    )
    assert isinstance(result.exc, AgentTermination)
    # Exactly one execute — the terminate INSERT — reached the mocked cursor:
    # the worker thread crossed the sys.modules cache into the ava module and
    # ava.DB.cursor() worked end-to-end.
    assert fake_cursor.execute.call_count == 1
    insert_sql = fake_cursor.execute.call_args.args[0]
    assert "inbound_messages" in insert_sql
    assert "'terminate'" in insert_sql
    assert "'self'" in insert_sql


async def test_run_in_thread_e2e_ava_self_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same as the terminate path, verifying AgentRestart's INSERT (kind='restart')."""
    from ava.self import AgentRestart

    fake_cursor = MagicMock()
    fake_cursor_cm = MagicMock()
    fake_cursor_cm.__enter__ = MagicMock(return_value=fake_cursor)
    fake_cursor_cm.__exit__ = MagicMock(return_value=None)
    fake_db = MagicMock()
    fake_db.cursor.return_value = fake_cursor_cm
    monkeypatch.setattr("ava.DB", fake_db)

    cancel_event = asyncio.Event()
    result = await _run_in_thread(
        code="import ava\nava.self.restart()",
        agent_id="1",
        cancel_event=cancel_event,
        timeout=5.0,
        chunk_publisher=None,
    )

    assert isinstance(result, _ExecLifecycle)
    assert isinstance(result.exc, AgentRestart)
    insert_sql = fake_cursor.execute.call_args.args[0]
    assert "'restart'" in insert_sql
