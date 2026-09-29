"""Worker acknowledgements for OTLP's bounded provider-flush boundary."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from typing import Any


class WorkerFlushMarker:
    """FIFO acknowledgement that the worker finished all earlier records."""

    def __init__(self) -> None:
        self.done = threading.Event()


def wait_for_worker(
    event_queue: queue.Queue[Any],
    timeout: float,
    report: Callable[[str], None],
) -> None:
    """Wait boundedly until the worker has emitted records before this marker."""
    marker = WorkerFlushMarker()
    deadline = time.monotonic() + timeout
    try:
        event_queue.put(marker, timeout=max(0.0, deadline - time.monotonic()))
    except queue.Full:
        report("flush marker timed out before the OTLP worker could receive it")
    else:
        if not marker.done.wait(max(0.0, deadline - time.monotonic())):
            report(f"flush timed out after {timeout}s waiting for the OTLP worker")


def stop_worker(
    event_queue: queue.Queue[Any], worker: threading.Thread, report: Callable[[str], None]
) -> None:
    """Request a bounded worker stop before the shutdown provider flush."""
    try:
        event_queue.put(None, timeout=2.0)
    except (AttributeError, queue.Full):
        report("shutdown marker timed out before the OTLP worker could receive it")
    else:
        worker.join(timeout=2.0)
        if worker.is_alive():
            report("shutdown timed out waiting for the OTLP worker")
