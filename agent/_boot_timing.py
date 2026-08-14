"""Boot-phase timing — make the import-dominated gap from process start to the
first graph step attributable instead of a black box.

The agent cold start is dominated by eager imports (langgraph + the ava SDK +
the LLM/redis stacks), amplified on a loaded host by page-cache misses and CPU
contention. `mark()` records a handful of coarse checkpoints along the boot
path; `emit()` logs one structured `boot_timing` line right before the graph
runs, so a creeping boot (import bloat, slow config fetch, large history load)
shows up as a named segment instead of an unobserved gap between `started_at`
and the first node.

Marks accumulate in one module-level list shared by `agent/__main__.py` (the
early `start` / `enter_starting` / `import` anchors) and `agent/loop.py:main`
(the post-import phases). `emit()` is the sole reset point.
"""

from __future__ import annotations

import time
from itertools import pairwise

# (phase, monotonic_ts) in call order; the first mark is the start anchor.
_marks: list[tuple[str, float]] = []


def mark(phase: str) -> None:
    """Record reaching `phase`. The first mark anchors the boot; each later mark
    closes the segment named after it."""
    _marks.append((phase, time.monotonic()))


def progress() -> tuple[int, str]:
    """How many phases the boot has reached, and the name of the last one.

    Read from `agent/_boot_deadline.py`'s watchdog thread, which is why this
    exists rather than that module reaching into `_marks`: the count is the boot's
    progress signal and the name is what the child reports when it gives up. A
    plain `list.append` and a `len()` are each atomic under the GIL, so a reader
    on another thread sees a count that is never torn — it may be one mark behind,
    which costs at most one extra poll interval of patience and never a false
    stall (the count only ever grows).

    Returns `(0, "")` before the first mark — the anchor is set in
    `agent/__main__.py` before anything can watch, so this is only reachable from
    a test that never marked.
    """
    marks = _marks  # one read of the module global; the list itself is append-only
    return (len(marks), marks[-1][0] if marks else "")


def emit() -> None:
    """Log one `boot_timing` line (per-segment ms + total) and reset. No-op when
    fewer than two marks were recorded (e.g. a test that drives `main` without
    the `__main__` anchors and bails before any phase mark)."""
    marks = _marks[:]
    _marks.clear()
    if len(marks) < 2:
        return
    from shared.log import logger

    segments = {b[0]: round((b[1] - a[1]) * 1000) for a, b in pairwise(marks)}
    total = round((marks[-1][1] - marks[0][1]) * 1000)
    logger.info(
        "boot reached first graph step in {total}ms",
        total=total,
        phases=segments,
        event="boot_timing",
    )
