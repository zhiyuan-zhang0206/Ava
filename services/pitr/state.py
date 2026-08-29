"""Observable state contract for local WAL durability versus remote acknowledgement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

HealthLevel = Literal["ok", "degraded", "critical"]


@dataclass(frozen=True)
class ArchiveHealth:
    local_archived_segments: int
    local_archived_bytes: int
    remote_acked_segments: int
    remote_acked_bytes: int
    unacked_segments: int
    unacked_bytes: int
    oldest_unacked_seconds: float | None
    last_remote_ack_lsn: str | None
    archive_errors_total: int
    quota_rejections_total: int
    upload_errors_total: int
    level: HealthLevel
    detail: str | None


def health_state(
    *,
    local_segments: int,
    local_bytes: int,
    remote_segments: int,
    remote_bytes: int,
    oldest_unacked_seconds: float | None,
    last_remote_ack_lsn: str | None,
    upload_errors_total: int,
    archive_errors_total: int,
    quota_rejections_total: int,
    warn_bytes: int,
    hard_bytes: int,
    warn_seconds: int,
    critical_seconds: int,
) -> ArchiveHealth:
    """Describe spool pressure without ever treating local archive as remote ACK."""
    values = (
        local_segments,
        local_bytes,
        remote_segments,
        remote_bytes,
        archive_errors_total,
        quota_rejections_total,
        upload_errors_total,
    )
    if any(value < 0 for value in values):
        raise ValueError("archive health counters cannot be negative")
    if remote_segments > local_segments or remote_bytes > local_bytes:
        raise ValueError("remote ACK counters cannot exceed local archive counters")
    unacked_segments = local_segments - remote_segments
    unacked_bytes = local_bytes - remote_bytes
    if unacked_bytes >= hard_bytes or (
        oldest_unacked_seconds is not None and oldest_unacked_seconds >= critical_seconds
    ):
        level: HealthLevel = "critical"
        detail = "local spool hard bound reached; PostgreSQL retains unarchived WAL in pg_wal"
    elif unacked_bytes >= warn_bytes or (
        oldest_unacked_seconds is not None and oldest_unacked_seconds >= warn_seconds
    ):
        level = "degraded"
        detail = "remote acknowledgement is behind local durable archive"
    else:
        level = "ok"
        detail = None
    return ArchiveHealth(
        local_archived_segments=local_segments,
        local_archived_bytes=local_bytes,
        remote_acked_segments=remote_segments,
        remote_acked_bytes=remote_bytes,
        unacked_segments=unacked_segments,
        unacked_bytes=unacked_bytes,
        oldest_unacked_seconds=oldest_unacked_seconds,
        last_remote_ack_lsn=last_remote_ack_lsn,
        archive_errors_total=archive_errors_total,
        quota_rejections_total=quota_rejections_total,
        upload_errors_total=upload_errors_total,
        level=level,
        detail=detail,
    )
