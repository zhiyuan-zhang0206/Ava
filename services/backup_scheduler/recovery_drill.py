"""Durable cadence for the weekly local logical-backup restore drill."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from services.backup import BACKUP_HOUR, _cluster_tz, backup_dir
from shared.private_storage import write_private_bytes

_WEEKLY_RESTORE_WEEKDAY = 6
_SUCCESS_MARKER = ".logical-restore-drill.json"


def local_dump_restore_due(now: datetime, *, last_success: datetime | None) -> bool:
    """Whether this week's post-backup restore window lacks a success record."""
    if now.tzinfo is None:
        raise ValueError("recovery drill needs a TZ-aware current time")
    scheduled = _weekly_window(now, _cluster_tz())
    if now < scheduled:
        return False
    return last_success is None or last_success < scheduled


def load_local_dump_restore_success(path: Path | None = None) -> datetime | None:
    """Read the one durable weekly-drill success marker, if one exists."""
    marker = path or backup_dir() / _SUCCESS_MARKER
    if not marker.exists():
        return None
    try:
        return _parse_success_marker(marker.read_text())
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("logical restore drill success marker is invalid") from exc


def record_local_dump_restore_success(now: datetime, path: Path | None = None) -> None:
    """Atomically publish a completed weekly-drill marker after verification."""
    if now.tzinfo is None:
        raise ValueError("recovery drill success time must be TZ-aware")
    marker = path or backup_dir() / _SUCCESS_MARKER
    payload = json.dumps(
        {"completed_at": now.astimezone(UTC).isoformat()}, sort_keys=True, separators=(",", ":")
    ).encode()
    write_private_bytes(marker, payload)


def _parse_success_marker(value: str) -> datetime:
    raw = json.loads(value)
    completed_at = raw["completed_at"]
    if set(raw) != {"completed_at"} or not isinstance(completed_at, str):
        raise ValueError("invalid marker shape")
    stamp = datetime.fromisoformat(completed_at)
    if stamp.tzinfo is None:
        raise ValueError("marker time is naive")
    return stamp.astimezone(UTC)


def _weekly_window(now: datetime, timezone: ZoneInfo) -> datetime:
    local_now = now.astimezone(timezone)
    days_since_sunday = (local_now.weekday() - _WEEKLY_RESTORE_WEEKDAY) % 7
    scheduled_day = local_now.date() - timedelta(days=days_since_sunday)
    scheduled = datetime.combine(
        scheduled_day,
        time(hour=BACKUP_HOUR),
        tzinfo=timezone,
    )
    if scheduled > local_now:
        scheduled -= timedelta(days=7)
    return scheduled.astimezone(UTC)
