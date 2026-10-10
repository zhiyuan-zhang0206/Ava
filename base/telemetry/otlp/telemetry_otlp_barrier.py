"""FIFO receipts belonging to the OTLP backend's sole writer."""

from __future__ import annotations

import threading


class WorkerFlushMarker:
    """A distinct receipt for all records preceding this marker in the queue."""

    def __init__(self) -> None:
        self.done = threading.Event()
