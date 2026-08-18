"""SDK-usage telemetry primitives — the runtime state + emit path behind the Metrics
page's SDK Usage panel, kept in ``shared`` so an SDK function body (``ava`` layer) can
enrich its own call while the wrapping machinery lives in ``agent/sdk_metering.py``.

One ``sdk_call`` event is written to the unified ``events`` stream (via the emitter) per
top-level ``ava.*`` invocation. Payload shape:

    {"event": "sdk_call", "fn": "<ns>.<fn>", "detail": {<semantic k/v>}, "duration": <s>}

``duration`` is the wall-clock seconds of the whole top-level call, measured by
``run_metered`` (declared by the registry's ``SdkCall`` TypedDict).

``fn`` is the count key (``shared.metrics_aggregate``'s sdk_usage groups on it) and the discriminator
for ``detail`` — the same discipline ``shared/live_events.py`` uses with ``role``. ``detail``
holds semantic facts an SDK function chooses to record about a specific call via
``annotate()`` — e.g. a shell helper noting that this run was a ``grep``. It defaults to
absent; the semantic layer fills per-``fn`` shapes later without changing the event
structure or needing a migration (``events.attributes`` is free-form JSONB).

``detail`` is derived from the call's **real runtime arguments** (ground truth) — it is
NOT, and must never regress to, a scan of the code's source text. The old metric matched
``ava.X(`` textually and so counted private calls, comments, strings, and example code;
this path only ever reflects a call that actually ran, with the arguments it ran with.

Discipline (shared with the recorder in ``agent/sdk_metering.py``): a pure side channel.
An emit / annotate failure is swallowed (``Exception`` only, so cancel/timeout injection
still propagates) and never changes an SDK call's arguments, result, or exceptions.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Generator, Mapping
from typing import Any

from shared.log import logger

# Event name written to events for one top-level SDK call.
SDK_CALL_EVENT = "sdk_call"

# Thread-local state, all per exec-worker thread:
#   .active — inside agent-authored code (armed by `recording()`); metering is off
#             elsewhere so framework-internal ava.* calls are never counted.
#   .frames — a stack of per-call `detail` dicts, one pushed per metered call. Its
#             length is the SDK-call nesting depth; only the depth-0 (bottom) call
#             emits an event. annotate() targets the top of the stack, so a nested
#             SDK call's annotations attach to its own frame and are discarded with
#             it (nested calls are not counted), never polluting the outer event.
_local = threading.local()


@contextlib.contextmanager
def recording() -> Generator[None, None, None]:
    """Arm SDK-call metering for the enclosed block — the exec worker wraps the
    ``exec(compile(agent_code))`` call with this, so only agent-authored ``ava.*``
    calls are metered. Framework-internal calls (system-prompt rendering, hooks) run
    outside it and are never counted. Reentrant: restores the previous flag on exit."""
    prev = getattr(_local, "active", False)
    _local.active = True
    try:
        yield
    finally:
        _local.active = prev


def annotate(**detail: Any) -> None:
    """Merge semantic key/values into the current SDK call's event ``detail``.

    Called by an SDK function's own body to enrich *its* ``sdk_call`` event with facts
    about this specific invocation (drawn from the real arguments) — e.g. a shell helper
    recording the sub-command it dispatched. Targets the innermost active call frame, so
    a nested SDK call annotates its own (discarded) frame, never the outer event. A no-op
    outside any metered call. Pure side channel: swallows all errors, never raises into
    the SDK call, never changes its result."""
    with contextlib.suppress(Exception):
        frames: list[dict[str, Any]] | None = getattr(_local, "frames", None)
        if frames:
            frames[-1].update(detail)


def emit(fn: str, detail: Mapping[str, Any] | None = None, duration: float | None = None) -> None:
    """Write one ``sdk_call`` event. Pure side channel — a broken log sink is swallowed
    and never raises into the SDK call path. ``detail`` is omitted from the payload when
    empty, so a plain call stays ``{fn}``; ``duration`` (seconds, measured by
    ``run_metered``) rides as a top-level payload key — the registry declares it
    (``contract.SdkCall``), so a reader may reference ``attributes->>'duration'``."""
    with contextlib.suppress(Exception):
        extra: dict[str, Any] = {"fn": fn}
        if detail:
            extra["detail"] = dict(detail)
        if duration is not None:
            extra["duration"] = duration
        logger.bind(event=SDK_CALL_EVENT, **extra).info("sdk_call")


def run_metered(fn: str, original: Callable[..., Any], args: Any, kwargs: Any) -> Any:
    """Run ``original(*args, **kwargs)`` as a metered SDK call.

    Outside ``recording()`` (framework-internal), calls straight through with no frame
    and no event. Inside it, pushes a ``detail`` frame for the duration of the call so
    the body's ``annotate()`` lands on this frame; only the outermost (depth-0) call
    emits an event, carrying whatever it accumulated — a nested call's frame is popped
    and discarded. The event is emitted after the call returns (so ``detail`` is
    complete), on success and on exception alike. The call's result and exceptions pass
    through untouched.
    """
    if not getattr(_local, "active", False):
        return original(*args, **kwargs)
    frames: list[dict[str, Any]] | None = getattr(_local, "frames", None)
    if frames is None:
        frames = _local.frames = []
    is_top = len(frames) == 0
    frames.append({})
    t0 = time.monotonic()
    try:
        return original(*args, **kwargs)
    finally:
        detail = frames.pop()
        if is_top:
            # Wall-clock seconds for the whole top-level call — the registry's
            # SdkCall payload declares `duration`; before this the TypedDict
            # key had no producer (audit-round2 events-obs P2).
            emit(fn, detail, duration=time.monotonic() - t0)
