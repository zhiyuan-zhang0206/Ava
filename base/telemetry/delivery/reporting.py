"""Best-effort diagnostics for the event pipeline: reports that never re-enter it.

Every drain-thread, sink and exit-time failure is reported through loguru with the
`_no_emitter` marker, so the emitter adapter skips the record and a failing pipeline cannot
loop its own report back into itself. This module imports nothing from `base.telemetry` so
every layer of the pipeline can report through it.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Generator
from typing import Any

# Loguru extra key marking the emitter's own diagnostics. Records carrying it
# are filtered OUT of the emitter adapter's sink (see
# `base.telemetry.process_logging.add_event_sink`) so they reach stderr / JSONL file sinks only
# and never re-enter this pipeline — a mirror-down process would otherwise loop
# failure → warning → emit → failure forever.
NO_EMITTER = "_no_emitter"


def report_no_pipeline(
    message: str, *, level: str = "warning", exc: BaseException | None = None, **extra: Any
) -> None:
    """Log a drain-thread diagnostic through loguru, marked `NO_EMITTER` so
    the emitter adapter skips it; `exc` attaches that exception's traceback.
    Never raises, never blocks — and never silent: when loguru itself fails the
    diagnostic goes to stderr."""
    try:
        from base.log import logger

        # **{NO_EMITTER: True} — bind() takes literal kwargs, so the marker
        # key must be the constant's VALUE, not its name.
        bound = logger.bind(**{NO_EMITTER: True})
        if exc is not None:
            bound = bound.opt(exception=exc)
        bound.log(level.upper(), message, **extra)
    except Exception as log_exc:
        try:
            sys.stderr.write(
                f"{level.upper()}: {message} {extra!r} "
                f"(diagnostic logging failed: {log_exc!r}; cause: {exc!r})\n"
            )
        except (OSError, ValueError):
            return  # stderr is closed or detached: no reporting channel is left


# Failures per emitter-plumbing seam, for `report_sink_failure`.
_sink_failures: dict[str, int] = {}


def report_sink_failure(sink: str, exc: BaseException) -> None:
    """Report a failure of a best-effort side channel (an emitter sink export,
    a missing producer, an exit-time close, SDK-call or billing telemetry)
    with its traceback — the first and every 50th per `sink`, like the JSONL
    mirror: a seam that fails on every call must be loud without flooding the
    log. Goes through the `_no_emitter` path, so a failing telemetry pipeline
    cannot loop its own report back into itself."""
    n = _sink_failures[sink] = _sink_failures.get(sink, 0) + 1
    if n == 1 or n % 50 == 0:
        report_no_pipeline(
            "[best-effort] {sink} failed ({n} time(s) in this process; the first and every "
            "50th are reported); carrying on without it: {err}",
            sink=sink,
            n=n,
            err=repr(exc),
            exc=exc,
        )


@contextlib.contextmanager
def failure_isolated(sink: str) -> Generator[None]:
    """Isolate one best-effort emitter seam: an `Exception` in the body must not
    reach the producer or the drain thread, and is reported, not swallowed."""
    try:
        yield
    except Exception as exc:
        report_sink_failure(sink, exc)
