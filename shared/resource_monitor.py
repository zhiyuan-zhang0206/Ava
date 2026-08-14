"""Per-machine resource sampling — CPU / memory / disk, stored in a ring buffer.

A module-level singleton collects a resource snapshot on every call to
`snapshot()` and appends it to a bounded deque (ring buffer). The buffer is
returned as a list of `ResourcePoint` models, serializable over the wire to the
status page.

Used by both the gateway's local `status_snapshot()` and the agent-runner ops
server's `cluster_status_op` — both paths share the same collector so resource
data is always per-machine. The collector is created lazily on the first call
and lives for the process lifetime (the ops daemon is long-running; the gateway
is long-running via uvicorn).
"""

from __future__ import annotations

import time
from collections import deque

from pydantic import BaseModel, ConfigDict

from shared.platform import primary_disk_path

MAX_SAMPLES = 300  # ~25 min at 5s poll interval


# The cross-process resource-sample contract: produced by the collector below,
# consumed by the gateway status page + the agent-runner ops server. The wire
# schemas (gateway.schemas.status) reference this one model. Kept in shared so
# both the producer and both consumers depend on a single definition (shared is
# the lowest layer; ops/gateway sit above it).
class ResourcePoint(BaseModel):
    """One resource sample — CPU / memory / disk at a point in time."""

    model_config = ConfigDict(frozen=True)

    ts: float  # unix timestamp
    cpu_pct: float  # CPU usage percent (0-100)
    mem_used_gb: float
    mem_total_gb: float
    mem_pct: float  # memory usage percent
    disk_used_gb: float
    disk_total_gb: float
    disk_pct: float  # disk usage percent


class ResourceCollector:
    """Samples CPU / memory / disk and holds the last `max_samples` snapshots."""

    def __init__(self, max_samples: int = MAX_SAMPLES) -> None:
        self._max = max_samples
        self._samples: deque[ResourcePoint] = deque(maxlen=max_samples)
        # Prime psutil.cpu_percent baseline — the first call with interval=None
        # returns 0.0 because there is no previous reading to compare against.
        # Calling it once now (discarding the result) establishes the baseline
        # so the first real snapshot gets a meaningful value.
        from contextlib import suppress

        with suppress(Exception):
            import psutil

            psutil.cpu_percent(interval=None)

    def snapshot(self) -> list[ResourcePoint]:
        """Take a reading and return the full buffer (newest last)."""

        import psutil

        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(primary_disk_path())

        # psutil's `percent` is the canonical fullness/pressure figure, but it is
        # measured against a *different* denominator than the naive `.total`, so
        # `used / total` disagreed with the shown percent (disk read 69% next to
        # an 84% bar, memory 34% next to 80%). Derive the displayed used/total
        # pair from that same denominator so the panel is self-consistent —
        # shown_used / shown_total == shown_percent:
        #   * disk.percent == used / (used + free)  (== `df` Capacity; the
        #     root-reserved blocks that pad `disk.total` are excluded), so the
        #     shown total is used + free, the space actually available to us.
        #   * mem.percent == (total - available) / total, where `available`
        #     counts reclaimable cache; `mem.used` alone undercounts on macOS, so
        #     the shown used is total - available (real pressure), total is RAM.
        disk_total = disk.used + disk.free
        mem_used = mem.total - mem.available

        point = ResourcePoint(
            ts=time.time(),
            cpu_pct=psutil.cpu_percent(interval=None),
            mem_used_gb=round(mem_used / (1024**3), 2),
            mem_total_gb=round(mem.total / (1024**3), 2),
            mem_pct=mem.percent,
            disk_used_gb=round(disk.used / (1024**3), 2),
            disk_total_gb=round(disk_total / (1024**3), 2),
            disk_pct=disk.percent,
        )
        self._samples.append(point)
        return list(self._samples)


_collector = ResourceCollector()


def resource_snapshot() -> list[ResourcePoint]:
    """Return this machine's resource history (newest last).

    Each call appends a fresh reading before returning the buffer, so the
    status page sees an up-to-date data point every poll. The per-process
    collector is created at module import time.
    """
    return _collector.snapshot()
