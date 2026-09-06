"""Per-slot at-most-once execution for gateway-hosted schedule templates.

The database claim commits before the callback starts. That ordering prevents
concurrent or restarted runners from repeating a slot, at the accepted cost
that a process crash after the claim and before the callback completes leaves
the slot claimed and will not retry it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Generic, TypeVar

import shared.db
from shared.db_transaction import write_transaction
from shared.watcher import previous_fire

_log = logging.getLogger(__name__)

MAX_CATCH_UP_SLOTS = 2

_Payload = TypeVar("_Payload")


@dataclass(frozen=True)
class _MissedSlot(Generic[_Payload]):
    fire_at: datetime
    payload: _Payload


def _schedule_id(explicit: int | None) -> int:
    schedule_id = int(os.environ["AVA_SCHEDULE_ID"]) if explicit is None else explicit
    if schedule_id <= 0:
        raise ValueError(f"schedule_id must be positive, got {schedule_id}")
    return schedule_id


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must carry tzinfo")
    return value.astimezone(UTC)


def _catch_up_baseline(schedule_id: int) -> datetime:
    """Return the newest claimed slot, or schedule creation on first use."""
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(f.slot_fire_at), s.created_at) "
            "FROM schedules AS s "
            "LEFT JOIN schedule_fire_log AS f ON f.schedule_id = s.id "
            "WHERE s.id = %s GROUP BY s.created_at",
            (schedule_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"schedule {schedule_id} does not exist")
    return _as_utc(row[0], field="catch-up baseline")


def fire_slot_once(
    slot_fire_at: datetime,
    payload: _Payload,
    *,
    fire: Callable[[_Payload], None],
    schedule_id: int | None = None,
) -> bool:
    """Claim one cron slot and invoke ``fire`` exactly when this caller wins.

    The durable claim gives concurrent processes and later restarts one winner.
    ``True`` means this caller claimed and invoked the callback; ``False`` means
    the slot was already claimed. Callback failures propagate while the claim
    remains committed, preserving the feature's explicit at-most-once posture.
    """
    resolved_id = _schedule_id(schedule_id)
    slot = _as_utc(slot_fire_at, field="slot_fire_at")
    with write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedule_fire_log (schedule_id, slot_fire_at) "
            "VALUES (%s, %s) ON CONFLICT (schedule_id, slot_fire_at) DO NOTHING "
            "RETURNING id",
            (resolved_id, slot),
        )
        claimed = cur.fetchone() is not None
    if not claimed:
        return False
    fire(payload)
    return True


def catch_up(
    triggers: Sequence[tuple[str, _Payload]],
    *,
    timezone: str | None,
    fire: Callable[[_Payload], None],
    now: datetime | None = None,
    limit: int = MAX_CATCH_UP_SLOTS,
    schedule_id: int | None = None,
) -> list[datetime]:
    """Fire at most the most recent ``limit`` missed cron slots on startup.

    The newest durable claim is the lower bound. On first use, schedule
    creation is the lower bound, so a schedule cannot replay time before it
    existed. Only ``limit + 1`` candidates per trigger are inspected: enough
    to select the global newest slots and detect truncation without scanning an
    arbitrarily old schedule history.
    """
    if limit <= 0:
        raise ValueError(f"catch-up limit must be positive, got {limit}")
    resolved_id = _schedule_id(schedule_id)
    current = _as_utc(now or datetime.now(UTC), field="now")
    baseline = _catch_up_baseline(resolved_id)
    by_fire_at: dict[datetime, _MissedSlot[_Payload]] = {}

    for expression, payload in triggers:
        cursor = current
        for _ in range(limit + 1):
            fire_at = previous_fire(expression, before=cursor, timezone=timezone)
            if fire_at <= baseline:
                break
            if fire_at not in by_fire_at:
                by_fire_at[fire_at] = _MissedSlot(fire_at=fire_at, payload=payload)
            cursor = fire_at

    ordered = sorted(by_fire_at.values(), key=lambda slot: slot.fire_at)
    if len(ordered) > limit:
        _log.warning(
            "schedule %s catch-up limited to the %s most recent slots; "
            "older missed slots remain unclaimed (baseline=%s, now=%s)",
            resolved_id,
            limit,
            baseline.isoformat(),
            current.isoformat(),
        )
        ordered = ordered[-limit:]

    fired: list[datetime] = []
    for slot in ordered:
        if fire_slot_once(
            slot.fire_at,
            slot.payload,
            fire=fire,
            schedule_id=resolved_id,
        ):
            fired.append(slot.fire_at)
    return fired
