"""Bounded Loki sharding for the cluster status surface."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

_LOKI_SHARD = timedelta(hours=3)
_LOKI_SHARD_WORKERS = 4


def split_loki_window(
    from_: datetime,
    to: datetime,
    *,
    shard_width: timedelta = _LOKI_SHARD,
) -> list[tuple[datetime, datetime]]:
    """Split a retained live read into contiguous, clock-aligned bounded spans."""
    if from_ >= to:
        return []
    if shard_width <= timedelta():
        raise ValueError("shard_width must be positive")
    spans: list[tuple[datetime, datetime]] = []
    start = from_
    shard_s = int(shard_width.total_seconds())
    while start < to:
        next_boundary_s = ((int(start.timestamp()) // shard_s) + 1) * shard_s
        end = min(datetime.fromtimestamp(next_boundary_s, tz=UTC), to)
        spans.append((start, end))
        start = end
    return spans


def query_loki_shards[T](
    from_: datetime,
    to: datetime,
    query: Callable[[datetime, datetime], T],
    *,
    shard_width: timedelta = _LOKI_SHARD,
) -> list[T]:
    """Run bounded Loki spans concurrently; each query acquires Loki's global slot."""
    spans = split_loki_window(from_, to, shard_width=shard_width)
    if not spans:
        return []
    if len(spans) == 1:
        start, end = spans[0]
        return [query(start, end)]
    with ThreadPoolExecutor(max_workers=min(_LOKI_SHARD_WORKERS, len(spans))) as executor:
        futures = [executor.submit(query, start, end) for start, end in spans]
        return [future.result() for future in futures]
