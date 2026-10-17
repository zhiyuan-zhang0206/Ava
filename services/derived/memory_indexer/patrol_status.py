from __future__ import annotations

import logging
import threading
import time
from typing import TypedDict

from base import telemetry
from services.derived.memory_indexer.config import MemoryIndexerConfig

_log = logging.getLogger("services.derived.memory_indexer.patrol_status")


class IndexLag(TypedDict):
    pending_paths: int
    last_success_at: float | None
    consecutive_failures: int
    over_limit: bool


class PatrolStatus:
    def __init__(self, *, retry_limit: int) -> None:
        self._lock = threading.Lock()
        self._retry_limit = retry_limit
        self._pending = 0
        self._last_success_at: float | None = None
        self._consecutive_failures = 0
        self._over_limit = False

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    @property
    def over_limit(self) -> bool:
        with self._lock:
            return self._over_limit

    def begin(self, pending: int) -> None:
        with self._lock:
            self._pending = pending

    def settled(self, paths: int) -> None:
        with self._lock:
            self._pending = max(0, self._pending - paths)

    def failed(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            self._over_limit = self._consecutive_failures > self._retry_limit

    def succeeded(self) -> None:
        with self._lock:
            self._pending = 0
            self._last_success_at = time.time()
            self._consecutive_failures = 0
            self._over_limit = False

    def lag(self) -> IndexLag:
        with self._lock:
            return IndexLag(
                pending_paths=self._pending,
                last_success_at=self._last_success_at,
                consecutive_failures=self._consecutive_failures,
                over_limit=self._over_limit,
            )

    def health_fields(self) -> dict[str, object]:
        return {"index": self.lag()}


def retry_delay_s(config: MemoryIndexerConfig, failures: int) -> float:
    delay = config.retry_backoff_s
    for _ in range(failures - 1):
        delay = min(delay * 2, config.patrol_interval_s)
    return delay


def emit_patrol_event(status: PatrolStatus) -> None:
    lag = status.lag()
    attributes: dict[str, object] = {
        "pending_paths": lag["pending_paths"],
        "consecutive_failures": lag["consecutive_failures"],
        "over_limit": lag["over_limit"],
    }
    if lag["last_success_at"] is not None:
        attributes["last_success_at"] = lag["last_success_at"]
    try:
        telemetry.emit("telemetry", "memory_index_patrol", attributes=attributes)
    except Exception:
        _log.warning("[indexer] patrol event emit failed", exc_info=True)
