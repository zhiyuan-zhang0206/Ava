"""Exec worker-thread mechanics shared with `_exec.py`: async-exception
injection into a worker thread, and the bounded reaper that kills orphaned
threads stuck in native code (Task #1058).

Split out of `_exec.py` (2026-08) when the reaper pushed that file over the
800-line ceiling: this is the "raw thread" half (ctypes injection + reaping),
while `_exec.py` keeps the node logic (polling, streams, result dispatch).
No import cycle: this module imports only stdlib + shared.log.
"""

from __future__ import annotations

import ctypes
import threading
import time

from shared.log import logger

# The orphan reaper's cadence and budget: re-inject KeyboardInterrupt every
# interval while the orphaned thread is alive, giving up after the window.
# Sized so a thread whose native call returns is killed within one interval of
# that return, while a thread stuck in an infinite native loop (or an
# `except BaseException` swallow loop) costs at most one bounded reaper thread
# per orphan — itself daemon, so it never blocks process exit.
_THREAD_REAP_INTERVAL_S = 1.0
_THREAD_REAP_WINDOW_S = 60.0


def _async_raise_in_thread(tid: int, exc_type: type[BaseException]) -> None:
    """Make CPython interpreter raise exc_type at the target thread's next Python bytecode boundary.

    PyThreadState_SetAsyncExc is a CPython C API not exposed at the Python
    level, must be called via ctypes. Returns 1 = success; 0 = thread state
    does not exist (thread already exited naturally, having raced with caller's
    `t.is_alive()` and won — caller takes the natural-completion path no-op);
    >1 = dispatched to multiple thread states (should not happen under CPython
    implementation, one tid corresponds to one thread state; this means
    interpreter bug).

    Response latency:
        - Pure Python infinite loop (`while True: x += 1`): responds at the
          next eval-breaker check (backward jump / func call boundary) —
          CPython 3.10+ does not check every bytecode, but loop back-edges
          always check.
        - Blocking syscall (`time.sleep` / `socket.recv` / `requests.get`):
          when the syscall returns naturally, interp immediately checks
          pending async exc → raises. `time.sleep` in CPython implementation
          periodically returns to interp to check signals, sub-second response.
        - Pure native C call (numpy big matrix / ctypes / cython nogil):
          while GIL is released the interp has no chance to run, **no response**.
          Thread orphaned; daemon=True lets OS clean up on main process exit.
    """
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(tid), ctypes.py_object(exc_type)
    )
    if res > 1:
        # Defensive: PyThreadState_SetAsyncExc >1 means it dispatched to multiple
        # threads; CPython docs require immediately resetting to prevent state
        # corruption. Should not happen under CPython implementation (one tid
        # corresponds to one thread state); if it really happens it's an
        # interpreter bug, raise.
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), None)
        raise SystemError(f"PyThreadState_SetAsyncExc injection failed tid={tid}")
    # res == 0: thread already exited naturally; caller main task on the next
    # poll sees t.is_alive()=False and takes the natural-completion branch,
    # no need to warn here — the race has already been won.


def _reap_orphaned_thread(t: threading.Thread, agent_id: str) -> None:
    """Kill an orphaned exec thread the moment its native call returns.

    The deadline/cancel injection is one-shot: a thread inside `time.sleep`
    does not see it until the sleep returns, and code that swallows
    `Exception` (TimeoutError is one) keeps running forever. So after the
    framework has already declared the exec over and moved on, re-inject
    KeyboardInterrupt on a cadence — it is a BaseException, so `except
    Exception` swallow loops cannot survive it — until the thread dies or the
    window elapses. Bounded: a thread that survives the window (infinite
    native code, or an `except BaseException` swallow loop) stays a daemon
    orphan, freed only at process exit. Runs in its own daemon thread; the
    reaper must never outlive or block the agent.
    """
    deadline = time.monotonic() + _THREAD_REAP_WINDOW_S
    while t.is_alive() and time.monotonic() < deadline:
        if t.ident is not None:
            try:
                _async_raise_in_thread(t.ident, KeyboardInterrupt)
            except SystemError:
                break  # tid vanished mid-raise; the is_alive() re-check exits
        time.sleep(_THREAD_REAP_INTERVAL_S)
    if t.is_alive():
        logger.warning(
            "[{label}] exec thread {agent_id} survived the reap window — "
            "infinite native code or an `except BaseException` swallow loop; "
            "daemon orphan, freed at process exit. Long-running agent code "
            "should use ava.watcher / ava.shell.sessions instead of sleeping "
            "in execute_code",
            label="exec-thread-unreapable",
            event="exec_thread_unreapable",
            agent_id=agent_id,
        )
