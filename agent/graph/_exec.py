"""exec node: agent-written code runs in a worker thread inside the main process (no longer subprocess).

All paths return Command(goto="after_exec") — under cycling topology after_exec
always routes to claim, which decides whether to wait or continue multi-step
based on pending inbound + state.halted.

Core mechanisms:
  - Main process has already `import ava`-ed at startup (sys.modules cache);
    agent code in the worker thread `import ava.X` hits the frozen snapshot —
    breaking ava/*.py on disk does not affect the in-process copy (unless
    agent explicitly `importlib.reload`s, explicit footgun not defended).
  - `_run_in_thread` spawns a worker thread that runs `exec(compile(code))`;
    the main task polls in an asyncio while-loop every 50ms: thread alive /
    cancel_event / deadline. cancel/timeout is injected into the thread via
    `ctypes.pythonapi.PyThreadState_SetAsyncExc` with `KeyboardInterrupt` /
    `TimeoutError` (CPython C API, raises at the next Python bytecode boundary).
    Pure Python infinite loops / blocking syscalls all respond; pure native
    code stuck does not respond, the thread is orphaned (Task #1058) — but
    only after a bounded background reaper has spent a window re-injecting
    KeyboardInterrupt on a cadence: the one-shot injection is invisible to a
    thread inside `time.sleep`, and code that swallows `Exception` (TimeoutError
    is one) survives it forever. KeyboardInterrupt is a BaseException, so only
    an infinite native loop or an `except BaseException` swallow loop leaks to
    process exit (daemon=True lets the OS clean those up at exit).
  - Halt signal uses exception type rather than exit code: agent code raising
    `_LifecycleExit` (AgentTermination / AgentRestart / _SystemHalt) → captured
    in result_holder["lifecycle"] → exec_node decides halted + writes marker
    based on isinstance.
  - stdout/stderr is redirected to `StreamingTextIO` (manual assignment +
    a conditional restore in `finally`, so a reaped orphan's late unwind
    cannot clobber a newer exec's streams); the main task polls every 50ms and
    pushes the new accumulated chunk to redis (frontend streaming display).
    The same stream catches stdout+stderr to preserve chronological order —
    same as what running Python in a terminal shows. Accumulation is bounded
    by `exec_output_accumulation_max_chars`: past it the middle is dropped as
    it streams and a `StreamCap` rides the result to the envelope, so a
    runaway print loop is truncated rather than left to OOM the process —
    the run itself is not killed.
  - `_exec_with_cancel_event` returns a sum type (`_ExecDone | _ExecCancelled |
    _ExecTimedOut | _ExecLifecycle | _ExecCrashed`); exec_node dispatches via
    `match`, illegal state combinations are unrepresentable. Ordinary exception
    tracebacks are already in the stream output.

State type hint key design (`state: _state.AgentState` + `from __future__ import
annotations`): LangGraph 1.x narrows the state schema by the node function's
first parameter type hint — `from agent.state import AgentState` statically
captures the alias (at module load time = BaseAgentState); after build_graph,
`agent.state.AgentState` is rebound to the dynamic subclass with plugin
fields, but this module's `AgentState` name is already snapshotted, and
LangGraph sees BaseAgentState with only 2 channels and drops all plugin
fields. Changed to `from agent import state as _state` + use
`_state.AgentState` to do module-attribute dynamic lookup; combined with
future annotations to defer annotation resolution to get_type_hints()
evaluation time, by which build_agent_state has rebound and we get the
dynamic AgentState. Pyright statically still sees `_state.AgentState` as
`BaseAgentState` (`agent/state.py` end has `AgentState = BaseAgentState`
alias), so `state.messages` / `state.halted` still type-check; plugin fields
are accessed dynamically (consistent with existing convention).
"""

from __future__ import annotations

import asyncio
import contextvars
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, TextIO

from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

import ava
from agent import state as _state
from agent.graph._exec_notes import merge_exec_notes
from agent.messages import exec_output_message
from agent.state import _validate_plugin_state_keys
from ava.security import take_findings
from shared import sdk_telemetry
from shared.config import settings
from shared.exit_codes import IDLE_EXIT_CODE, SYSTEM_HALT_EXIT_CODE
from shared.lifecycle import (
    AgentRestart,
    AgentTermination,
    _LifecycleExit,
    _SystemHalt,
)
from shared.live_events import (
    Cancelled,
    ExecOutput,
    ExecStart,
)
from shared.log import logger

from ._agent_traceback import (
    format_agent_traceback,
    format_full_traceback,
    register_agent_source,
)
from ._context import AvaContext, agent_id_from_config
from ._exec_output import wrap_code_output
from ._exec_stream import ExecOutputChunkPublisher, StreamCap, StreamingTextIO
from ._exec_threads import _async_raise_in_thread, _reap_orphaned_thread
from ._interrupt import subscribe_interrupt
from ._node_log import node_lifecycle
from ._nodes import AFTER_EXEC
from ._tool_calls import merge_multiple_execute_code_tool_calls

# exec_node always goto AFTER_EXEC (under the cycling topology, halted is routed by after_exec)
ExecGoto = Literal["after_exec"]

# Main asyncio task polling interval for monitoring thread alive / cancel / deadline.
# 50ms is near-free from CPU's perspective; cancel/timeout response latency is
# ≤ 50ms + the latency for ctypes injection to reach the next bytecode boundary
# (microseconds), imperceptible to humans.
_POLL_INTERVAL_S = 0.05

# Grace period after ctypes-injecting an exception into the thread, letting it
# print traceback + run finally chain cleanup (close socket / let SDK internal
# finally clean up an already-launched ava.shell.run subprocess). If the thread
# is still alive beyond this, native code is stuck; mark the thread orphaned
# (daemon=True takes over), main task returns the envelope letting LLM / user decide.
_THREAD_JOIN_GRACE_S = 2.0
# Orphan reaper cadence/budget + _async_raise_in_thread live in
# `_exec_threads.py` (Task #1058) — see there.
_THREAD_STUCK_WINDOW_S, _THREAD_STUCK_WARN_THRESHOLD = 3600.0, 3
_thread_stuck_times: deque[float] = deque(maxlen=_THREAD_STUCK_WARN_THRESHOLD)


# `_exec_with_cancel_event` returns a sum type — 5 mutually exclusive variants + a shared output field.
# Frozen dataclass + match dispatch replaces the old NamedTuple `(output, exc,
# cancelled, timed_out)` 4-bool state space — illegal combinations (cancelled=True
# while timed_out=True, or cancelled=True while exc is a lifecycle) are unrepresentable
# at the type level; exec_node uses match for pyright exhaustiveness check instead
# of hand-written `assert not (cancelled and timed_out)` mutual-exclusion guards.
#
# Lifecycle priority is implemented in `_run_in_thread`'s end construction
# order (if a lifecycle exc exists, construct `_ExecLifecycle` directly, skipping
# the cancelled/timed_out branches) — moving the "lifecycle always wins" race
# decision from dispatch site to construction site; exec_node match no longer
# has to consider the race.


# Every variant carries `stream_cap`: set when the accumulation budget dropped
# the middle of `output` mid-run (see `_exec_stream.StreamingTextIO`). Every
# outcome can be capped — a runaway loop can also time out or be cancelled — so
# the field rides the whole sum type rather than one branch, and
# `_dispatch_exec_result` hands it to the envelope uniformly.
@dataclass(frozen=True)
class _ExecDone:
    """Worker thread completed, no exception."""

    output: str
    stream_cap: StreamCap | None = None


@dataclass(frozen=True)
class _ExecCancelled:
    """User pressed Stop → main task ctypes-injects KeyboardInterrupt →
    thread exits; `output` contains accumulated partial + KeyboardInterrupt traceback."""

    output: str
    stream_cap: StreamCap | None = None


@dataclass(frozen=True)
class _ExecTimedOut:
    """60s deadline → main task ctypes-injects TimeoutError → thread exits."""

    output: str
    stream_cap: StreamCap | None = None


@dataclass(frozen=True)
class _ExecLifecycle:
    """Agent actively called `ava.self.{terminate,restart,compact}` raising
    `_LifecycleExit` subclass — lifecycle takes priority over cancel/timeout:
    on a same-tick race, lifecycle has already INSERTed the inbound via SDK,
    framework should respect this semantic rather than downgrading to
    "was interrupted"."""

    output: str
    exc: _LifecycleExit  # _SystemHalt | AgentTermination | AgentRestart
    stream_cap: StreamCap | None = None


@dataclass(frozen=True)
class _ExecCrashed:
    """Non-lifecycle exception (SyntaxError / NameError / missing import /
    other SystemExit subclasses / user code raising KeyboardInterrupt|TimeoutError, etc.).
    Traceback is already in output; exec_node logs at INFO + event=exec_failed — an
    ordinary dev-flow error, not an operator alert."""

    output: str
    exc: BaseException
    stream_cap: StreamCap | None = None


@dataclass(frozen=True)
class _ExecCall:
    """Resolved execute_code invocation; `state_messages_update` carries the
    merged tool-call message (when the multi-call merge fired)."""

    code: str
    tool_call_id: str
    state_messages_update: list[AnyMessage]


type _ExecResult = _ExecDone | _ExecCancelled | _ExecTimedOut | _ExecLifecycle | _ExecCrashed


def _exec_worker(
    code: str, stream: StreamingTextIO, result_holder: dict[str, BaseException | None]
) -> None:
    """Worker-thread body: `exec(compile(code))` with stdout/stderr captured.

    The same stream catches stdout and stderr, preserving print → traceback →
    print chronological order, same as running Python in a terminal.
    """
    fresh_globals: dict[str, Any] = {
        "__name__": "__agent_code__",
        "__builtins__": __builtins__,
    }
    pre_stdout, pre_stderr = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = stream
    try:
        # `recording()` arms SDK-usage metering for exactly this agent-authored
        # code, so framework-internal ava.* calls (prompt rendering, hooks) that
        # run outside it are never counted (see shared/sdk_telemetry.py).
        with sdk_telemetry.recording():
            exec(compile(code, "<agent_code>", "exec"), fresh_globals)
    except _LifecycleExit as e:
        # Lifecycle (terminate/restart/halt): SDK already INSERTed the
        # inbound; don't print a traceback (not an error).
        result_holder["exc"] = e
    except BaseException as e:
        # Ordinary exceptions / SystemExit / KeyboardInterrupt (cancel
        # injection) / TimeoutError (timeout injection) — exec_node dispatches
        # by cancelled/timed_out flag + exc type. Only the agent's own
        # `<agent_code>` frames go into the stream (agent-facing surface);
        # the full traceback reaches the logs via the _ExecCrashed branch.
        result_holder["exc"] = e
        stream.write(format_agent_traceback(e))
    finally:
        # A reaped orphan unwinds this redirect long after the framework has
        # already restored the process-global streams — and possibly a newer
        # exec has replaced them again. Restore only what this thread itself
        # still owns, so a late unwind cannot clobber a live exec's streams.
        if sys.stdout is stream:
            sys.stdout = pre_stdout
        if sys.stderr is stream:
            sys.stderr = pre_stderr


async def _poll_worker_loop(
    t: threading.Thread,
    stream: StreamingTextIO,
    chunk_publisher: ExecOutputChunkPublisher | None,
    cancel_event: asyncio.Event,
    deadline: float,
) -> tuple[bool, bool]:
    """Poll the worker thread every 50ms: publish accumulated stream chunks,
    ctypes-inject KeyboardInterrupt (user Stop) / TimeoutError (deadline).

    Returns (cancelled, timed_out); on a same-tick race cancel always wins
    (priority enforced by the result construction in `_run_in_thread`).
    """
    cancelled = False
    timed_out = False
    while t.is_alive():
        # Incrementally publish accumulated stream chunks to frontend
        if chunk_publisher is not None:
            pending = stream.take_pending()
            if pending:
                chunk_publisher.publish(pending)

        if cancel_event.is_set():
            # user pressed Stop; tid guaranteed non-None after t.start()
            assert t.ident is not None  # noqa: S101
            _async_raise_in_thread(t.ident, KeyboardInterrupt)
            cancelled = True
            break
        if time.monotonic() > deadline:
            assert t.ident is not None  # noqa: S101
            _async_raise_in_thread(t.ident, TimeoutError)
            timed_out = True
            break
        await asyncio.sleep(_POLL_INTERVAL_S)
    return cancelled, timed_out


async def _await_worker_exit(
    t: threading.Thread,
    agent_id: str,
    pre_stdout: TextIO,
    pre_stderr: TextIO,
) -> None:
    """Join the worker with a grace period; a thread still alive past it is
    stuck in native code — log, orphan (daemon=True), restore the streams."""
    if not t.is_alive():
        return
    await asyncio.to_thread(t.join, _THREAD_JOIN_GRACE_S)
    if not t.is_alive():
        return
    _thread_stuck_times.append(time.monotonic())
    if len(_thread_stuck_times) >= _THREAD_STUCK_WARN_THRESHOLD and (
        time.monotonic() - _thread_stuck_times[0] <= _THREAD_STUCK_WINDOW_S
    ):
        logger.warning(
            "[{label}] {n} threads stuck in native code in the last hour",
            label="exec-thread-stuck",
            n=len(_thread_stuck_times),
            event="exec_thread_stuck",
            agent_id=agent_id,
        )
    # The orphaned thread never ran its redirect's __exit__, so restore
    # the process-global streams here. Otherwise sys.stderr stays the
    # fileno-less capture buffer and the next node's stall-dump
    # diagnostic (faulthandler) would fault on it, killing the agent.
    sys.stdout, sys.stderr = pre_stdout, pre_stderr
    # Hand the orphan to the bounded reaper (Task #1058): the one-shot
    # injection cannot kill a thread inside a native call or one that
    # swallows Exception, so re-inject KeyboardInterrupt on a cadence until
    # the thread dies or the window elapses.
    threading.Thread(
        target=_reap_orphaned_thread,
        args=(t, agent_id),
        daemon=True,
        name=f"exec-reap-{agent_id}",
    ).start()


def _construct_exec_result(
    output: str,
    exc: BaseException | None,
    *,
    cancelled: bool,
    timed_out: bool,
    stream_cap: StreamCap | None,
) -> _ExecResult:
    """Dispatch priority: lifecycle > cancel > timeout > crashed > done.

    Lifecycle always wins (the agent actively calling terminate/restart/compact
    has higher semantic priority than "was interrupted"; SDK already INSERTed
    inbound). Cancel > timeout (CLAUDE design: cancel always wins).
    """
    if isinstance(exc, _LifecycleExit):
        return _ExecLifecycle(output=output, exc=exc, stream_cap=stream_cap)
    if cancelled:
        return _ExecCancelled(output=output, stream_cap=stream_cap)
    if timed_out:
        return _ExecTimedOut(output=output, stream_cap=stream_cap)
    if exc is not None:
        return _ExecCrashed(output=output, exc=exc, stream_cap=stream_cap)
    return _ExecDone(output=output, stream_cap=stream_cap)


async def _run_in_thread(
    code: str,
    agent_id: str,
    cancel_event: asyncio.Event,
    timeout: float,
    chunk_publisher: ExecOutputChunkPublisher | None,
) -> _ExecResult:
    """Run `exec(compile(code))` in a worker thread; main asyncio task polls
    every 50ms monitoring thread alive / cancel_event / deadline.

    Priority: cancel_event > deadline > thread natural completion. Cancel
    always wins on a same-tick race (user Stop takes effect immediately).
    """
    stream = StreamingTextIO()
    result_holder: dict[str, BaseException | None] = {"exc": None}

    # Register the source so `<agent_code>` frames resolve their offending line
    # in tracebacks (exec'd code is invisible to linecache).
    register_agent_source(code)

    # Captured before the worker redirects them: an orphaned thread (stuck in
    # native code) never unwinds its redirect, so the orphan branch restores
    # the process-global streams here.
    pre_stdout, pre_stderr = sys.stdout, sys.stderr
    # Run the worker under a copy of the creating context: threads do NOT
    # inherit contextvars, and in the hosted runner the turn identity
    # (shared/turn_identity.py) and the per-turn config view
    # (shared/config/turn_view.py) both live in contextvars that agent code on
    # this thread reads through ava.* / turn_settings. In process mode the
    # copied context carries nothing bound and behavior is unchanged.
    exec_ctx = contextvars.copy_context()
    t = threading.Thread(
        target=exec_ctx.run,
        args=(_exec_worker, code, stream, result_holder),
        daemon=True,
        name=f"exec-{agent_id}",
    )
    t.start()

    deadline = time.monotonic() + timeout
    cancelled, timed_out = await _poll_worker_loop(
        t, stream, chunk_publisher, cancel_event, deadline
    )
    await _await_worker_exit(t, agent_id, pre_stdout, pre_stderr)

    # Wrap up: publish remaining chunks from the stream (thread post-cleanup may still write traceback)
    if chunk_publisher is not None:
        pending = stream.take_pending()
        if pending:
            chunk_publisher.publish(pending)

    return _construct_exec_result(
        stream.getvalue(),
        result_holder["exc"],
        cancelled=cancelled,
        timed_out=timed_out,
        stream_cap=stream.cap(),
    )


async def _exec_with_cancel_event(
    code: str,
    agent_id: str,
    cancel_event: asyncio.Event,
    timeout: float | None = None,
    chunk_publisher: ExecOutputChunkPublisher | None = None,
) -> _ExecResult:
    """Run worker thread with three-way monitoring (cancel_event + timeout),
    return a unified `_ExecResult` sum type.

    A wrapper layer on top of _run_in_thread providing an entry name
    symmetric with the old subprocess model, so exec_node / tests do not
    need to change call sites. If _run_in_thread behavior needs extension
    in the future (e.g. capture thread crash dump), add it at this layer.
    """
    if timeout is None:
        timeout = settings.sandbox.exec_timeout_seconds

    return await _run_in_thread(
        code=code,
        agent_id=agent_id,
        cancel_event=cancel_event,
        timeout=timeout,
        chunk_publisher=chunk_publisher,
    )


async def exec_node(
    state: _state.AgentState,
    runtime: Runtime[AvaContext],
    config: RunnableConfig,
) -> Command[ExecGoto]:
    """Run worker thread to execute agent-written code + ctypes async raise cancel/timeout.
    See module docstring."""
    event_publisher = runtime.context.event_publisher
    assert event_publisher is not None, "exec_node requires ctx.event_publisher"  # noqa: S101
    async with node_lifecycle(
        "exec",
        messages=state.messages,
        ops_pool=runtime.context.ops_pool,
        event_publisher=event_publisher,
        agent_id=agent_id_from_config(config),
    ):
        return await _exec_node_impl(state, runtime, config)


def _resolve_exec_call(state: _state.AgentState, agent_id: int) -> _ExecCall | Command[ExecGoto]:
    """Extract the execute_code tool call from the previous AIMessage.

    Single-tool wire format: the model must call Python via tool_calls[0]
    (bare content form is deprecated); tool_call_id pairs with the ToolMessage
    sent back. Returns an error Command (unknown-tool ToolMessage so the next
    round can retry), raises ValueError on the no-tool_calls path, else the
    resolved `_ExecCall`.
    """
    last = state.messages[-1]
    fixed_last = (
        merge_multiple_execute_code_tool_calls(
            last,
            agent_id=agent_id,
            location="exec_node",
        )
        if isinstance(last, AIMessage)
        else None
    )
    state_messages_update: list[AnyMessage] = []
    if fixed_last is not None:
        last = fixed_last
        state_messages_update.append(fixed_last)
    tool_calls = getattr(last, "tool_calls", None)
    if not tool_calls:
        raise ValueError(
            f"exec_node: previous AIMessage has no tool_calls (type={type(last).__name__}). "
            f"Model must call the execute_code tool; should not reach this path"
        )
    # Anthropic-compat providers (e.g. DeepSeek) don't grammar-constrain the
    # tool name to the registered set, so the model can hallucinate calling SDK
    # functions like `ava.files.edit` as tools. Surface as ToolMessage so the
    # next round can retry, instead of letting `["code"]` KeyError-crash exec.
    first = tool_calls[0]
    if first["name"] != "execute_code" or "code" not in first["args"]:
        err = exec_output_message(
            content=f"unknown tool {first['name']!r}; only `execute_code(code: str)` is registered",
            tool_call_id=first["id"],
            exit_code=0,
            created_at=datetime.now(UTC),
        )
        state_messages_update.append(err)
        return Command[ExecGoto](
            update={"messages": state_messages_update, "halted": False},
            goto=AFTER_EXEC,
        )
    return _ExecCall(
        code=first["args"]["code"],
        tool_call_id=first["id"],
        state_messages_update=state_messages_update,
    )


async def _run_agent_code(
    state: _state.AgentState,
    ctx: AvaContext,
    agent_id: int,
    code: str,
    chunk_publisher: ExecOutputChunkPublisher,
) -> tuple[_ExecResult, dict[str, Any], int]:
    """Run the agent's code in the worker thread under the plugin state slot.

    Sets `ava.state` / `ava.state_update` around the run (try/finally reset),
    races the run against the durable-interrupt cancel_event with the graph-level
    node timeout as an outer shield, and validates the plugin's state_update
    keys. Returns (result, plugin_state_update, exec_ms).
    """
    # ── plugin <-> framework state slot injection ──────────────────────────
    #
    # Worker thread and the LangGraph node share one process + `sys.modules['ava']`:
    # the node sets `ava.state` (model_copy(deep=True) — plugin edits never pollute
    # real state) and `ava.state_update` (merged into Command(update=...) at turn
    # end via the LangGraph reducer). try/finally guards reset both (including the
    # model_copy exception path), else an exec exception would leave the next turn
    # with a stale snapshot — breaking the "ava.state is None outside exec turn"
    # contract. Re-evaluate this module-level slot model if a parallel branch
    # (LangGraph fan-out) is ever introduced; the cycling topology has no race.
    plugin_state_update: dict[str, Any] = {}
    try:
        ava.state = state.model_copy(deep=True)
        ava.state_update = {}
        exec_started = time.monotonic()
        async with subscribe_interrupt(ctx.ops_pool, agent_id) as cancel_event:
            try:
                result = await asyncio.wait_for(
                    _exec_with_cancel_event(
                        code, str(agent_id), cancel_event, chunk_publisher=chunk_publisher
                    ),
                    timeout=settings.sandbox.exec_node_timeout_seconds,
                )
            except TimeoutError:
                # Graph-level exec node timeout — defense-in-depth above the
                # per-code-block exec_timeout_seconds. If the inner deadline
                # missed a hang inside the execution machinery (e.g. a stuck
                # thread join), this outer shield catches it and surfaces as
                # a timeout result rather than leaving the agent process stuck.
                # (The interrupt subscription sits outside this wait_for and is
                # bounded by its own watcher exit timeout.)
                logger.error(
                    "[exec(node-timeout)] exec_node timed out after {timeout}s — "
                    "inner code-exec timeout did not trigger; possible framework hang. "
                    "Returning timeout ToolMessage so the LLM can react.",
                    event="exec_node_timeout",
                    timeout=settings.sandbox.exec_node_timeout_seconds,
                    agent_id=agent_id,
                )
                result = _ExecTimedOut(
                    output=(
                        f"[exec node timeout after {settings.sandbox.exec_node_timeout_seconds:.0f}s] "
                        "Execution was stopped by an internal safeguard and did not "
                        "complete. This does not necessarily mean your code was slow; "
                        "consider re-running it, or moving long-running work to a "
                        "persistent shell session."
                    )
                )
        # Wall-clock surfaced on the code_output item ("ran in 1.3s"); cancel /
        # timeout still report the honest time-before-stop.
        exec_ms = round((time.monotonic() - exec_started) * 1000)
        # fail-fast: plugin abuse in the worker thread (setting to None / list / str)
        # blows up immediately; silent `or {}` fallback would swallow all of the
        # plugin's deltas this round (CLAUDE.md forbids the `... or default` pattern).
        if not isinstance(ava.state_update, dict):
            raise TypeError(
                f"plugin tampered with ava.state_update: expected dict, got {type(ava.state_update).__name__}"
            )
        plugin_state_update = _validate_plugin_state_keys(dict(ava.state_update), state.__class__)
    finally:
        ava.state = None
        ava.state_update = None
    return result, plugin_state_update, exec_ms


def _dispatch_exec_result(
    result: _ExecResult, ctx: AvaContext, agent_id: int
) -> tuple[bool, str, int]:
    """Map the `_ExecResult` sum type to (halted, result_text, exit_code_for_msg).

    Lifecycle priority (lifecycle always wins the cancel/timeout race) is
    implemented at the construction site in `_run_in_thread`; the match directly
    consumes the sum type. Exhaustiveness: pyright strict + match narrowing make
    a forgotten variant a static error (replaces the hand-written fallthrough).
    """
    # Present on every variant (see the sum-type definitions): when the
    # accumulation budget dropped the middle mid-run, the envelope needs it to
    # report the true produced length and to stop calling the archive complete.
    stream_cap = result.stream_cap
    match result:
        case _ExecLifecycle(output=output, exc=_SystemHalt()):
            # ava.self.compact already INSERTed compact_summary inbound; append
            # "[system halt]" at the end (agent's real output comes first).
            halted = True
            extra = "[system halt] You just called ava.self.compact; your context has been compacted and you will continue as the same agent\n"
            output = (output if not output or output.endswith("\n") else output + "\n") + extra
            result_text = wrap_code_output(output, stream_cap=stream_cap)
            exit_code_for_msg = SYSTEM_HALT_EXIT_CODE
            logger.info("[{label}] {body}", label="exec", body=result_text)
            logger.info("[{label}] {body}", label="halt", body="system_halt (compact)")
        case _ExecLifecycle(output=output, exc=AgentTermination() | AgentRestart() as exc):
            # SDK already INSERTed the inbound; the claim side writes the
            # lifecycle marker — no "[halt]" annotation (duplication is noise).
            halted = True
            result_text = wrap_code_output(output, stream_cap=stream_cap)
            exit_code_for_msg = IDLE_EXIT_CODE
            logger.info("[{label}] {body}", label="exec", body=result_text)
            logger.info(
                "[{label}] {body}",
                label="halt",
                body=f"lifecycle {type(exc).__name__}",
            )
        case _ExecLifecycle(exc=other_exc):
            # Exhaustive fallthrough: future _LifecycleExit subclass not handled
            # in the two cases above falls here and raises — safer than silently
            # taking the "ordinary exception" halted=False path. Implements
            # CLAUDE.md "enum dispatch must be exhaustive".
            raise TypeError(
                f"Unrecognized _LifecycleExit subclass: {type(other_exc).__name__!r} — "
                f"dispatch ladder missed update"
            )
        case _ExecCancelled(output=output):
            halted = True
            result_text = wrap_code_output(output, cancelled=True, stream_cap=stream_cap)
            exit_code_for_msg = -1
            logger.info(
                "[{label}] {body}", label="exec-cancelled", body=result_text, event="exec_cancelled"
            )
            # Notify frontend of abort (symmetric with llm_node cancel path;
            # the timeout path does not send Cancelled — not a user cancel).
            assert ctx.event_publisher is not None  # noqa: S101 — asserted by caller; narrowed for the emit
            ctx.event_publisher.emit(Cancelled(agent_id=agent_id).model_dump_json())
        case _ExecTimedOut(output=output):
            # Timeout is ordinary feedback, not a stop-turn signal: the envelope
            # hints at long-running primitives; the next LLM round adapts.
            halted = False
            result_text = wrap_code_output(output, timed_out=True, stream_cap=stream_cap)
            exit_code_for_msg = -1
            logger.info(
                "[{label}] {body}", label="exec-timeout", body=result_text, event="exec_timeout"
            )
        case _ExecCrashed(output=output, exc=exc):
            # Ordinary exception: `output` carries the agent-facing (filtered)
            # traceback; the log gets the full unfiltered chain (framework/SDK
            # bugs invisible in the agent view stay diagnosable). INFO +
            # event=exec_failed — trial-and-error is the normal dev loop, not
            # an operator alert (metrics still aggregate by event name).
            halted = False
            result_text = wrap_code_output(output, stream_cap=stream_cap)
            exit_code_for_msg = 0
            logger.info(
                "[{label}] {body}\n[full traceback]\n{full_traceback}",
                label="exec-failed",
                body=result_text,
                full_traceback=format_full_traceback(exc),
                event="exec_failed",
                exc_type=type(exc).__name__,
            )
        case _ExecDone(output=output):
            halted = False
            result_text = wrap_code_output(output, stream_cap=stream_cap)
            exit_code_for_msg = 0
            logger.info("[{label}] {body}", label="exec", body=result_text)
    return halted, result_text, exit_code_for_msg


async def _exec_node_impl(
    state: _state.AgentState,
    runtime: Runtime[AvaContext],
    config: RunnableConfig,
) -> Command[ExecGoto]:
    """Body of `exec_node`, extracted so `node_lifecycle` can wrap an enter/exit event."""
    ctx = runtime.context
    assert ctx.event_publisher is not None, (  # noqa: S101
        "_exec_node_impl requires ctx.event_publisher"
    )
    agent_id = agent_id_from_config(config)

    # exec_msg_idx = position the ToolMessage(exec_output) will land at — after
    # exec_node returns Command, LangGraph appends, so `len(state.messages)` is
    # that position. Computed before ExecStart so the frontend creates the
    # code_output placeholder as soon as exec begins.
    exec_msg_idx = len(state.messages)
    ctx.event_publisher.emit(
        ExecStart(agent_id=agent_id, item_id=f"{exec_msg_idx}.0").model_dump_json()
    )

    resolved = _resolve_exec_call(state, agent_id)
    if isinstance(resolved, Command):
        # Unknown-tool path: the error ToolMessage is already in the update list.
        return resolved
    state_messages_update = resolved.state_messages_update

    # Streaming chunks and the final ExecOutput share the same item_id
    # computed above; the frontend uses it to append chunks to the same
    # code_output item; on completion, ExecOutput upserts at the same id,
    # replacing with wrap_code_output envelope version.
    chunk_publisher = ExecOutputChunkPublisher(
        ctx.event_publisher,
        agent_id,
        item_id=f"{exec_msg_idx}.0",
    )

    result, plugin_state_update, exec_ms = await _run_agent_code(
        state, ctx, agent_id, resolved.code, chunk_publisher
    )
    halted, result_text, exit_code_for_msg = _dispatch_exec_result(result, ctx, agent_id)

    # Pop the plugin's messages delta out of the state update — merged below
    # after the ToolMessage instead of riding the dict **spread (which would
    # clobber the ToolMessage). Popped + drained unconditionally so a compact
    # turn (REMOVE_ALL'd by claim) leaks nothing to later turns.
    plugin_messages = plugin_state_update.pop("messages", None)
    findings = take_findings()

    # Compact path (_SystemHalt): write nothing back — claim REMOVE_ALLs the
    # whole history this turn, so ToolMessage/notes would be wiped anyway.
    if not (isinstance(result, _ExecLifecycle) and isinstance(result.exc, _SystemHalt)):
        # The UI shows exactly what the agent sees in exec output — same blob
        # fed back to the LLM below (ExecOutput shares item_id with the chunk).
        ctx.event_publisher.emit(
            ExecOutput(
                agent_id=agent_id,
                item_id=f"{exec_msg_idx}.0",
                content=result_text,
            ).model_dump_json()
        )

        msg = exec_output_message(
            content=result_text,
            tool_call_id=resolved.tool_call_id,
            exit_code=exit_code_for_msg,
            cancelled=isinstance(result, _ExecCancelled),
            timed_out=isinstance(result, _ExecTimedOut),
            exec_ms=exec_ms,
            created_at=datetime.now(UTC),
        )
        state_messages_update.append(msg)

        # In-memory system-note injection (user ruling 2026-08-11): security
        # findings + plugin context notes merge into this exec's delta, after
        # the ToolMessage (ordering rationale: _exec_notes.py).
        state_messages_update = merge_exec_notes(state_messages_update, plugin_messages, findings)
    return Command[ExecGoto](
        update={
            "messages": state_messages_update,
            "halted": halted,
            **plugin_state_update,
        },
        goto=AFTER_EXEC,
    )
