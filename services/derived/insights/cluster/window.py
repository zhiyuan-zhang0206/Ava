"""The time window of a cluster read and the bucket width that suits it."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException

# Bucket widths in seconds. Buckets are aligned to the epoch (not to the window), so panning
# the window never moves a bucket edge and a bucket keeps its value from one read to the next.
_NICE_SECONDS = (
    1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
    3600, 7200, 14400, 21600, 43200, 86400, 172800, 604800,
)  # fmt: skip


def parse_window(from_: datetime, to: datetime) -> tuple[datetime, datetime]:
    """The validated window; both ends carry a timezone offset and `from_` is earlier."""
    for name, value in (("from", from_), ("to", to)):
        if value.tzinfo is None:
            raise HTTPException(status_code=422, detail=f"{name} must include a timezone offset")
    if from_ >= to:
        raise HTTPException(status_code=422, detail="from must be earlier than to")
    return from_, to


def bucket_seconds(span_seconds: float, target: int) -> int:
    """The narrowest round width that covers `span_seconds` in at most `target` buckets."""
    if target < 1:
        raise ValueError("target must be at least 1")
    wanted = span_seconds / target
    for width in _NICE_SECONDS:
        if width >= wanted:
            return width
    return _NICE_SECONDS[-1]


def bucket_start(index: int, width: int) -> datetime:
    """The start of the epoch-aligned bucket `index` of `width` seconds."""
    return datetime.fromtimestamp(index * width, tz=UTC)
