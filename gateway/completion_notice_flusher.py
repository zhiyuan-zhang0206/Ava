"""Gateway-owned hourly completion-notice digest delivery."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from psycopg_pool import ConnectionPool

from gateway.routers._delivery import deliver_chat_inbound
from shared.completion_notices import (
    CompletionDigest,
    format_digest,
    mark_digest_delivered,
    pending_digests,
    prune_delivered_notices,
)

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

    This function is owned only by the gateway lifespan task. A crash after
    delivery but before the mark repeats the same idempotency key, obtaining
    the existing inbound receipt instead of a second digest.
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


async def completion_notice_flusher(pool: ConnectionPool) -> None:
    """Run the gateway's sole periodic completion-digest flush loop."""
    while True:
        try:
            await flush_once(pool)
        except Exception:
            _log.warning("completion-notice digest flush failed", exc_info=True)
        await asyncio.sleep(FLUSH_INTERVAL_S)
