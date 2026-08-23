"""Frozen `events` archive boundary shared by gateway read paths.

The archive stopped receiving writes at the 2026-08-13 Loki cutover and has no
`ts` index. Its `SELECT max(ts)` was measured as a 2.9M-row, 1.93-second scan
on 2026-08-23, so the result is correct to cache for this process lifetime.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

_condition = threading.Condition()
_loaded = False
_loading = False
_boundary: datetime | None = None


def frozen_boundary() -> datetime | None:
    """Return the cached frozen-archive boundary without querying Postgres."""
    with _condition:
        return _boundary if _loaded else None


def load_frozen_boundary(cur: Any) -> datetime | None:
    """Load `max(events.ts)` once, sharing a concurrent first request's scan."""
    global _boundary, _loaded, _loading  # noqa: PLW0603 — process-lifetime archive fact
    with _condition:
        if _loaded:
            return _boundary
        while _loading:
            _condition.wait()
            if _loaded:
                return _boundary
        _loading = True

    try:
        # The scan must happen outside the lock: another caller waits on the
        # condition rather than serializing unrelated cache readers behind I/O.
        cur.execute("SELECT max(ts) FROM events")
        row = cur.fetchone()
        boundary = row[0] if row is not None else None
    except Exception:
        with _condition:
            _loading = False
            _condition.notify_all()
        raise

    with _condition:
        _boundary = boundary
        _loaded = True
        _loading = False
        _condition.notify_all()
        return _boundary


def reset_for_tests(*, force: bool = True) -> None:
    """Clear the process cache for a fresh test database when requested."""
    global _boundary, _loaded, _loading  # noqa: PLW0603 — intentional test seam
    if not force:
        return
    with _condition:
        _boundary = None
        _loaded = False
        _loading = False
        _condition.notify_all()
