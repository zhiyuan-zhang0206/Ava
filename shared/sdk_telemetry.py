"""SDK-call events and optional per-execution tallies.

Every outermost wrapped SDK call emits by default, including external Python and
framework callers. Live sampling policy affects events only. ``recording()``
collects a full tally for an execute_code result; it never gates instrumentation.
Semantic details come from real calls via ``annotate()``, never source scanning.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    # Annotation-only (`sdk_calls_by_tool_call_id`); the runtime isinstance
    # imports ToolMessage at its call site. This module loads on the exec-child
    # boot path (`_run_code`), which must stay off the LangChain message stack
    # (task #3633; `_TYPE_CHECKING_ALLOWED`).
    from langchain_core.messages import BaseMessage

from shared.message_kwargs import AvaMsgType, read_ava_kwargs

# Event name written to events for one top-level SDK call.
SDK_CALL_EVENT = "sdk_call"


@dataclass
class _CallFrame:
    fn: str
    detail: dict[str, Any] = field(default_factory=dict[str, Any])


_frames: ContextVar[tuple[_CallFrame, ...]] = ContextVar("sdk_frames", default=())
_tally: ContextVar[dict[str, int] | None] = ContextVar("sdk_tally", default=None)
_identity: ContextVar[dict[str, Any] | None] = ContextVar("sdk_identity", default=None)


@contextlib.contextmanager
def recording() -> Generator[dict[str, int], None, None]:
    """Collect full top-level counts for an execution block, independently of events."""
    tally: dict[str, int] = {}
    token = _tally.set(tally)
    try:
        yield tally
    finally:
        _tally.reset(token)


def annotate(**detail: Any) -> None:
    """Merge semantic key/values into the current SDK call's event ``detail``.

    Called by an SDK function's own body to enrich *its* ``sdk_call`` event with facts
    about this specific invocation (drawn from the real arguments) — e.g. a shell helper
    recording the sub-command it dispatched. Targets the innermost active call frame, so
    a nested SDK call annotates its own (discarded) frame, never the outer event. A no-op
    outside any metered call. Pure side channel: swallows all errors, never raises into
    the SDK call, never changes its result."""
    with contextlib.suppress(Exception):
        frames = _frames.get()
        if frames:
            frames[-1].detail.update(detail)


def emit(fn: str, detail: Mapping[str, Any] | None = None, duration: float | None = None) -> None:
    """Write one ``sdk_call`` event. Pure side channel — a broken log sink is swallowed
    and never raises into the SDK call path. ``detail`` is omitted from the payload when
    empty, so a plain call stays ``{fn}``; ``duration`` (seconds, measured by
    ``run_metered``) rides as a top-level payload key — the registry declares it
    (``contract.SdkCall``), so a reader may reference ``attributes->>'duration'``."""
    with contextlib.suppress(Exception):
        from shared.sdk_call_policy import policy

        current = policy()
        every = current.sample_every if current.sampling_enabled else 1
        if every > 1:
            import random

            if random.randrange(every) != 0:  # noqa: S311 — telemetry sampling, not security
                return
        extra: dict[str, Any] = {"fn": fn, "sample_rate": every}
        if detail:
            extra["detail"] = dict(detail)
        if duration is not None:
            extra["duration"] = duration
        from shared import telemetry

        telemetry.emit("telemetry", SDK_CALL_EVENT, attributes=extra, **(_identity.get() or {}))


@contextlib.contextmanager
def _manifest_sdk_capture_admission() -> Generator[None, None, None]:
    """Use the optional local manifest gate without changing SDK call behavior."""
    try:
        from shared.agents.impersonation_manifest import admitted_local_sdk_call
    except Exception:
        # Manifest instrumentation is a side channel. An unavailable settings
        # bootstrap must never turn an SDK operation into a new hard failure.
        yield
        return
    with admitted_local_sdk_call():
        yield


@contextlib.contextmanager
def _measure(fn: str) -> Generator[None, None, None]:
    frames = _frames.get()
    # Reinstalled recorders around plugin layers share one public call frame.
    # Keep semantic annotations from the original function, without duplicate rows.
    if frames and frames[-1].fn == fn:
        yield
        return
    # A controller may close while this call is in its body.  Admit before
    # entering it, then retain that admission through the `finally` emission
    # so the local manifest cannot seal between the call and its sdk_call row.
    with _manifest_sdk_capture_admission():
        frame = _CallFrame(fn)
        token = _frames.set((*frames, frame))
        t0 = time.monotonic()
        try:
            yield
        finally:
            _frames.reset(token)
            if not frames:
                tally = _tally.get()
                if tally is not None:
                    tally[fn] = tally.get(fn, 0) + 1
                emit(fn, frame.detail, duration=time.monotonic() - t0)


def run_metered(fn: str, original: Callable[..., Any], args: Any, kwargs: Any) -> Any:
    """Record a synchronous invocation, preserving its return and exceptions."""
    with _measure(fn):
        return original(*args, **kwargs)


async def run_metered_async(
    fn: str, original: Callable[..., Awaitable[Any]], args: Any, kwargs: Any
) -> Any:
    """Record an async invocation when awaited, including cancellation and duration."""
    with _measure(fn):
        return await original(*args, **kwargs)


# ── the wire-side counts: entry model + materialization + read-back ───────────


class SdkCall(BaseModel):
    """One SDK method's call count in an agent_code block, e.g.
    ``SdkCall(method="files.read", count=3)`` for ``ava.files.read(...)`` x3."""

    method: str
    count: int


def tally_entries(tally: Mapping[str, int]) -> list[dict[str, Any]]:
    """Materialize a recording tally as the wire list that rides the exec result:
    ``[{"method": "<ns>.<fn>", "count": N}, ...]``, sorted by descending count then
    method — the collapsed-code chip's render order (JSON dicts, one per fn)."""
    entries: list[dict[str, Any]] = []
    for fn, count in sorted(tally.items(), key=lambda item: (-item[1], item[0])):
        entries.append({"method": fn, "count": count})
    return entries


def sdk_calls_by_tool_call_id(
    messages: Sequence[BaseMessage], start: int = 0
) -> dict[str, list[SdkCall]]:
    """Map each exec_output ToolMessage's ``tool_call_id`` to its block's SDK calls.

    The counts are the runtime tally ``agent/graph/_exec.py`` wrote to the message's
    ``additional_kwargs["sdk_calls"]`` — what the code really executed, never a scan
    of its text. Only ``messages[start:]`` is scanned (the rendered span): a tool
    call's metadata rides the ToolMessage that follows it, so nothing outside the
    span can enrich an item rendered from it. A message without the field (pre-tally
    history) stays absent from the map; a block that ran with zero SDK calls maps to
    ``[]`` — a real zero the UI must not confuse with "not yet known".
    """
    from langchain_core.messages import ToolMessage

    out: dict[str, list[SdkCall]] = {}
    for msg in messages[start:]:
        if not isinstance(msg, ToolMessage):
            continue
        kwargs = read_ava_kwargs(msg)
        if kwargs.get("ava_msg_type") != AvaMsgType.EXEC_OUTPUT:
            continue
        calls = kwargs.get("sdk_calls")
        if calls is None:
            continue
        out[msg.tool_call_id] = [SdkCall.model_validate(entry) for entry in calls]
    return out
