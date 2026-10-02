"""Bounded synchronization for the event emitter's close-time barrier."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from typing import Any


def synchronize(
    *,
    flush: Callable[[], None],
    event_queue: queue.Queue[Any],
    marker: object,
    drain_thread: Any,
    marker_done: threading.Event,
    timeout: float,
    bounded: bool,
) -> str | None:
    """Flush then acknowledge the drain thread, with one deadline when bounded."""
    if bounded:
        flushed = threading.Event()

        def flush_before_close() -> None:
            try:
                flush()
            finally:
                flushed.set()

        threading.Thread(target=flush_before_close, daemon=True, name="event-emitter-sync").start()
        if not flushed.wait(timeout):
            return "flush"
    else:
        flush()
    if threading.current_thread() is drain_thread or not drain_thread.is_alive():
        return None
    deadline = time.monotonic() + timeout
    marker_done.clear()
    try:
        event_queue.put(marker, timeout=max(0.0, deadline - time.monotonic()))
    except queue.Full:
        return "marker"
    return None if marker_done.wait(max(0.0, deadline - time.monotonic())) else "drain"
