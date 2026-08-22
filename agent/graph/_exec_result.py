"""Exec outcome sum type — the mutually exclusive result variants one
`execute_code` run produces, plus the priority constructor and the
parent-side placeholder for a crash that happened in a child process.

Moved out of `agent/graph/_exec.py` (2026-08, exec-subprocess work) so the
subprocess machinery (`agent/graph/_exec_subprocess.py`) can construct the
same sum type the exec node dispatches without importing `_exec.py` (which
would close an import cycle once `_exec.py` itself imports the machinery).
`_exec.py` re-exports every name here, so existing callers and tests keep
their imports.

`ExecChildError` is the one variant carrier: a real exception cannot
cross a process boundary, so a crashed child ships `exc_type` / `exc_msg` /
`full_traceback` as strings in the result envelope, and the parent wraps them
in this Exception subclass. `_ExecCrashed.full_traceback` (optional) carries
the child-formatted text; when it is None (a parent-side construction failure
— spawn error, unserializable state) the dispatcher falls back to
`format_full_traceback(exc)`.

Every variant carries `stream_cap` (set when the accumulation budget dropped
the middle of `output` mid-run) so both execution paths hand the cap to the
envelope uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.lifecycle import (
    AgentRestart,
    AgentTermination,
    _LifecycleExit,
    _SystemHalt,
)

from ._exec_stream import StreamCap


class ExecChildError(Exception):
    """A crash inside the exec child, re-raised parent-side with the child's
    traceback text. The child's real exception object cannot cross the process
    boundary; its type, message, and formatted traceback ride the result
    envelope instead."""

    def __init__(self, exc_type: str, exc_msg: str, full_traceback: str | None) -> None:
        self.exc_type = exc_type
        self.exc_msg = exc_msg
        self.full_traceback = full_traceback
        super().__init__(exc_msg or exc_type)


@dataclass(frozen=True)
class _ExecDone:
    """Execution completed, no exception."""

    output: str
    stream_cap: StreamCap | None = None


@dataclass(frozen=True)
class _ExecCancelled:
    """User pressed Stop -> child got SIGINT -> KeyboardInterrupt -> exit;
    `output` contains accumulated partial output + KeyboardInterrupt traceback."""

    output: str
    stream_cap: StreamCap | None = None


@dataclass(frozen=True)
class _ExecTimedOut:
    """Deadline -> child got SIGTERM -> TimeoutError -> exit (or was SIGKILLed
    past the grace period)."""

    output: str
    stream_cap: StreamCap | None = None


@dataclass(frozen=True)
class _ExecLifecycle:
    """Agent actively called `ava.self.{terminate,restart,compact}`, raising a
    `_LifecycleExit` subclass inside the child — lifecycle takes priority over
    cancel/timeout. The exception is reconstructed parent-side from the
    envelope's `lifecycle_type` name (fixed three-class map)."""

    output: str
    exc: _LifecycleExit  # _SystemHalt | AgentTermination | AgentRestart
    stream_cap: StreamCap | None = None


@dataclass(frozen=True)
class _ExecCrashed:
    """Non-lifecycle exception (SyntaxError / NameError / missing import /
    SystemExit subclasses / user code raising KeyboardInterrupt|TimeoutError,
    etc.). Traceback is already in output; exec_node logs at INFO +
    event=exec_failed. When the crash happened in the exec child,
    `full_traceback` carries the child-formatted text (the `exc` is an
    `ExecChildError` placeholder); None means a parent-side construction
    failure and the dispatcher formats from `exc`."""

    output: str
    exc: BaseException
    full_traceback: str | None = None
    stream_cap: StreamCap | None = None


type _ExecResult = _ExecDone | _ExecCancelled | _ExecTimedOut | _ExecLifecycle | _ExecCrashed

# The fixed set of lifecycle classes the child can report by name. The subprocess
# parent turns a missing name into an ExecChildError crash; the dispatcher's
# exhaustive TypeError separately guards any in-process `_LifecycleExit` callers.
_LIFECYCLE_BY_NAME: dict[str, type[_LifecycleExit]] = {
    cls.__name__: cls for cls in (AgentTermination, AgentRestart, _SystemHalt)
}


def lifecycle_exception_from_name(name: str) -> _LifecycleExit | None:
    """Instantiate the lifecycle exception a child reported by name.

    Return None for an unknown class name so the subprocess parent can surface
    a protocol crash. The dispatcher's exhaustive TypeError remains the guard
    for in-process callers carrying an unknown `_LifecycleExit` subclass.
    """
    cls = _LIFECYCLE_BY_NAME.get(name)
    return cls() if cls is not None else None


def _construct_exec_result(
    output: str,
    exc: BaseException | None,
    *,
    cancelled: bool,
    timed_out: bool,
    stream_cap: StreamCap | None = None,
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
