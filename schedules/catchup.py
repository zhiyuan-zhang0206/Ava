"""Per-slot at-most-once execution for gateway-hosted schedule templates.

The database claim commits before the callback starts. That ordering prevents
concurrent or restarted runners from repeating a slot, at the accepted cost
that a process crash after the claim and before the callback completes leaves
the slot claimed and will not retry it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

from base.clock import Clock
from base.db import Database


_Payload = TypeVar("_Payload")


def cluster_timezone() -> str:
    """The cluster wall-clock timezone (`MEIVA_TIMEZONE`, cluster-pinned), never the host's OS
    timezone: the whole fleet fires at one instant regardless of where each machine sits. A
    weekly cron is the case where a host-local reading lands the run on the wrong CALENDAR
    DAY, not merely at the wrong hour. The schedule script reads it when it runs; a changed
    value reaches a schedule at its next restart (`meiva schedules restart <id>`)."""
    return Clock.from_settings().timezone


def _schedule_id(explicit: int | None) -> int:
    schedule_id = int(os.environ["MEIVA_SCHEDULE_ID"]) if explicit is None else explicit
    if schedule_id <= 0:
        raise ValueError(f"schedule_id must be positive, got {schedule_id}")
    return schedule_id


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must carry tzinfo")
    return value.astimezone(UTC)


def fire_slot_once(
    db: Database,
    slot_fire_at: datetime,
    payload: _Payload,
    *,
    fire: Callable[[datetime, _Payload], None],
    schedule_id: int | None = None,
) -> bool:
    """Claim one cron slot and invoke ``fire`` exactly when this caller wins.

    The durable claim gives concurrent processes and later restarts one winner.
    The callback receives the claimed UTC slot and its payload after commit.
    ``True`` means this caller claimed and invoked the callback; ``False`` means
    the slot was already claimed. Callback failures propagate while the claim
    remains committed, preserving the feature's explicit at-most-once posture.
    """
    resolved_id = _schedule_id(schedule_id)
    slot = _as_utc(slot_fire_at, field="slot_fire_at")
    with db.write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedule_fire_log (schedule_id, slot_fire_at) "
            "VALUES (%s, %s) ON CONFLICT (schedule_id, slot_fire_at) DO NOTHING "
            "RETURNING id",
            (resolved_id, slot),
        )
        claimed = cur.fetchone() is not None
    if not claimed:
        return False
    fire(slot, payload)
    return True
