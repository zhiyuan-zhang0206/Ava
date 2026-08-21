"""In-process worker-thread exec backend — retained behind
`AVA_EXEC_BACKEND=thread` as the instant rollback valve while the subprocess
backend is the default; PR3 deletes this module.

Moved out of `_exec.py` (PR2) so the exec node's wiring layer does not carry
both backends' machinery: `_run_in_thread` runs `exec(compile(code))` in a
worker thread with context-bound output capture, the main asyncio task polls
every 50ms and ctypes-injects KeyboardInterrupt (cancel) / TimeoutError
(deadline), and a stuck thread is orphaned to the bounded reaper
(`_exec_threads.py`, Task #1058).
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from collections import deque
from typing import Any

from shared import sdk_telemetry
from shared.lifecycle import _LifecycleExit
from shared.log import logger

from ._agent_traceback import (
    format_agent_traceback,
    register_agent_source,
)
from ._exec_capture import capture_output, install_capture_routers
from ._exec_result import _construct_exec_result, _ExecResult
from ._exec_stream import ExecOutputChunkPublisher, StreamingTextIO
from ._exec_threads import _async_raise_in_thread, _reap_orphaned_thread

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
    # Capture is bound in THIS context only (see agent/graph/_exec_capture.py):
    # concurrent execs in one hosted process never see each other's output, and
    # an orphaned worker that never unwinds cannot leave the process-global
    # streams pointed at its own buffer.
    with capture_output(stream):
        try:
            # `recording()` arms SDK-usage metering for exactly this
            # agent-authored code, so framework-internal ava.* calls (prompt
            # rendering, hooks) that run outside it are never counted (see
            # shared/sdk_telemetry.py).
            with sdk_telemetry.recording():
                exec(compile(code, "<agent_code>", "exec"), fresh_globals)
        except _LifecycleExit as e:
            # Lifecycle (terminate/restart/halt): SDK already INSERTed the
            # inbound; don't print a traceback (not an error).
            result_holder["exc"] = e
        except BaseException as e:
            # Ordinary exceptions / SystemExit / KeyboardInterrupt (cancel
            # injection) / TimeoutError (timeout injection) — exec_node
            # dispatches by cancelled/timed_out flag + exc type. Only the
            # agent's own `<agent_code>` frames go into the stream
            # (agent-facing surface); the full traceback reaches the logs via
            # the _ExecCrashed branch.
            result_holder["exc"] = e
            stream.write(format_agent_traceback(e))


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


async def _await_worker_exit(t: threading.Thread, agent_id: str) -> None:
    """Join the worker with a grace period; a thread still alive past it is
    stuck in native code — log it and orphan it (daemon=True)."""
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
    # Nothing to restore: the orphan's capture binding lives in its own
    # context (agent/graph/_exec_capture.py), so the process's sys.stdout /
    # sys.stderr keep resolving to the real streams for everyone else — the
    # next node's faulthandler stall dump still finds a real fd.
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

    # Point the process streams at the routers before the worker binds its
    # capture. Idempotent and one-way — a router with nothing bound in the
    # calling context writes straight through to the real stream.
    install_capture_routers()
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
    await _await_worker_exit(t, agent_id)

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


__all__ = ["_run_in_thread"]
