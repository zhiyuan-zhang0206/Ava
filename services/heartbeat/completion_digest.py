"""Hourly completion-notice digest delivery, a loop of the heartbeat service.

Background-command and watcher completions are recorded as `completion_notice_events`
rows; once an hour has completed, the loop delivers one digest per agent and hour as
a chat inbound (source `system:completion-digest`) and marks the rows. A system
notice never resurrects a terminated owner (`ops.lifecycle.resurrect_if_terminated`),
so the digest waits in the owner's queue like any other framework notification.

The delivery key `completion-digest:<agent>:<hour>` makes a crash between the delivery
and the mark exactly once: the retry meets the existing receipt. One bad digest is
logged and retried on the next tick without blocking the others; any other exception
ends the loop and, through the service's `TaskGroup`, the process.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from psycopg_pool import ConnectionPool

from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.daemon.schedules.completion_notices import (
    CompletionDigest,
    format_digest,
    mark_digest_delivered,
    pending_digests,
    prune_delivered_notices,
)
from gateway.agents.delivery import deliver_chat_inbound

_log = logging.getLogger(__name__)

FLUSH_INTERVAL_S = 60.0
_SOURCE = "system:completion-digest"
_EVENT_RETENTION = timedelta(days=7)


def _pending(pool: ConnectionPool, now: datetime) -> list[CompletionDigest]:
    with pool.connection() as conn:
        return pending_digests(conn, now)


def _mark_delivered(pool: ConnectionPool, digest: CompletionDigest, inbound_id: int) -> None:
    with pool.connection() as conn:
        mark_digest_delivered(conn, digest.event_ids, inbound_id)
        conn.commit()


def _prune_delivered(pool: ConnectionPool, before: datetime) -> int:
    with pool.connection() as conn:
        pruned = prune_delivered_notices(conn, before)
        conn.commit()
    return pruned


def _digest_key(digest: CompletionDigest) -> str:
    """A stable receipt key makes send-before-mark recovery exactly once."""
    return f"completion-digest:{digest.agent_id}:{digest.window_start.astimezone(UTC).isoformat()}"


async def flush_once(pool: ConnectionPool, *, now: datetime | None = None) -> int:
    """Deliver each completed hour, then mark its persisted event rows.

    A crash after delivery but before the mark repeats the same idempotency key,
    obtaining the existing inbound receipt instead of a second digest.
    """
    moment = now or datetime.now(UTC)
    digests = await asyncio.to_thread(_pending, pool, moment)
    delivered = 0
    for digest in digests:
        try:
            delivery = await deliver_chat_inbound(
                pool,
                digest.agent_id,
                prepare=lambda _conn, digest=digest: format_digest(
                    agent_id=digest.agent_id,
                    window_start=digest.window_start,
                    notices=digest.notices,
                ),
                source=_SOURCE,
                client_message_id=_digest_key(digest),
            )
            if delivery.inbound_id is None:
                _log.warning(
                    "completion-notice digest delivery returned no inbound receipt for agent %s, "
                    "hour %s",
                    digest.agent_id,
                    digest.window_start.isoformat(),
                )
                continue
            await asyncio.to_thread(_mark_delivered, pool, digest, delivery.inbound_id)
        except Exception:
            _log.warning(
                "completion-notice digest delivery failed for agent %s, hour %s",
                digest.agent_id,
                digest.window_start.isoformat(),
                exc_info=True,
            )
        else:
            delivered += 1
    await asyncio.to_thread(_prune_delivered, pool, moment - _EVENT_RETENTION)
    return delivered


async def completion_digest_loop(pool: ConnectionPool, progress: LoopProgress) -> None:
    """The completion-digest flush as a resident sequential loop: one pass at
    start, then every `FLUSH_INTERVAL_S`."""

    async def one_round() -> None:
        await flush_once(pool)

    await round_loop.run_rounds("completion-digest", progress, FLUSH_INTERVAL_S, one_round)
