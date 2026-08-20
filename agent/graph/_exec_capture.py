"""Per-context stdout/stderr capture for the exec worker.

Prerequisite 1 of Phase 1 in `future/infra/agent-runner-as-server.md`: the exec
node used to capture agent output by **assigning** the process-global
`sys.stdout` / `sys.stderr` for the duration of a run. One agent per process
made that exact; in the hosted runner two agents' execs overlap in one process,
so a global assignment means agent A's `print` lands in agent B's exec output.

The replacement keeps `sys.stdout` / `sys.stderr` pointed at one stable object
per stream — a **router** installed once and never removed — and moves the
per-run part into a contextvar. A write resolves the capture bound in the
*calling* context; unbound contexts (the framework's own code, other agents'
turns, the main asyncio task) reach the real stream underneath.

Why a contextvar and not a `threading.local`: the worker thread already starts
under `contextvars.copy_context()` (`agent/graph/_exec.py`, landed with turn
identity in p1b), and agent code that runs `asyncio.to_thread` / creates tasks
propagates the same context — so one binding covers the worker thread and
everything it spawns through the standard machinery. A thread-local would lose
the capture at the first `to_thread` hop.

Two properties fall out that the old assignment could not offer:

- **Isolation** — concurrent execs never see each other's streams, because
  neither one ever touches a global.
- **No leak on an orphan** — a worker stuck in native code is orphaned without
  unwinding its `finally` (`agent/graph/_exec.py:_await_worker_exit`). Under
  the old scheme that left the process-global `sys.stderr` pointed at a
  fileno-less capture buffer for the rest of the process. Now the orphan owns
  only its own context: the process's `sys.stderr` is the router, which keeps
  resolving to the real stream everywhere else.

Installation is lazy (first exec), never at import: `shared/log.py` registers
its stderr sink with the stream object it sees at init time, and a router
installed before that would route framework log lines into whichever agent's
capture happened to be bound.
"""

from __future__ import annotations

import contextlib
import io
import sys
from collections.abc import Generator, Iterable
from contextvars import ContextVar
from typing import Any, TextIO

# Either half of the routing: the real process stream (a TextIO) or an exec
# capture (`_exec_stream.StreamingTextIO`, an io.TextIOBase).
type _Stream = TextIO | io.TextIOBase

# The capture bound in the current context, or None = write through to the real
# stream. One var serves both streams: exec merges stdout and stderr into a
# single StreamingTextIO to preserve chronological order.
_ACTIVE_CAPTURE: ContextVar[_Stream | None] = ContextVar("ava_exec_capture", default=None)


class _CaptureRouter:
    """`sys.stdout` / `sys.stderr` stand-in: every attribute resolves against
    the context's capture when one is bound, else the real stream.

    Not an `io.TextIOBase` subclass on purpose — inheriting would give it
    concrete stubs (`fileno`, `encoding`, `isatty`, …) that shadow the real
    stream's, so a consumer asking the router about the terminal would get the
    base class's answer instead of the underlying one. Delegating everything
    keeps the router transparent: `sys.stderr.fileno()` from unbound code
    returns the process's real fd, exactly as before this indirection existed.
    """

    __slots__ = ("_fallback",)

    def __init__(self, fallback: _Stream) -> None:
        object.__setattr__(self, "_fallback", fallback)

    def _target(self) -> _Stream:
        capture = _ACTIVE_CAPTURE.get()
        if capture is not None:
            return capture
        target: _Stream = object.__getattribute__(self, "_fallback")
        return target

    # write/flush/writelines are spelled out rather than left to __getattr__:
    # they are the hot path (every `print` from agent code) and resolving them
    # through the generic delegation would rebind the bound method per call.
    def write(self, s: str) -> int:
        return self._target().write(s)

    def writelines(self, lines: Iterable[str]) -> None:
        self._target().writelines(lines)

    def flush(self) -> None:
        self._target().flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)


def install_capture_routers() -> None:
    """Point `sys.stdout` / `sys.stderr` at routers. Idempotent, one-way.

    Called before the first exec worker starts. There is no uninstall: the
    router is transparent when nothing is bound, so leaving it in place costs
    an attribute lookup and removes the whole "restore the globals" failure
    mode (a late-unwinding orphan clobbering a live exec's streams).
    """
    if not isinstance(sys.stdout, _CaptureRouter):
        sys.stdout = _CaptureRouter(sys.stdout)  # pyright: ignore[reportAttributeAccessIssue]
    if not isinstance(sys.stderr, _CaptureRouter):
        sys.stderr = _CaptureRouter(sys.stderr)  # pyright: ignore[reportAttributeAccessIssue]


@contextlib.contextmanager
def capture_output(stream: _Stream) -> Generator[None]:
    """Bind `stream` as the current context's stdout+stderr capture.

    The exec worker wraps its `exec(compile(code))` in this. Only the calling
    context is affected, so an abandoned worker cannot leave the binding behind
    for anyone else — including the same agent's next turn.
    """
    token = _ACTIVE_CAPTURE.set(stream)
    try:
        yield
    finally:
        _ACTIVE_CAPTURE.reset(token)


def current_capture() -> _Stream | None:
    """The capture bound in the current context, or None."""
    return _ACTIVE_CAPTURE.get()
