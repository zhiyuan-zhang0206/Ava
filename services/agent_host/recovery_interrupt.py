"""Observe durable interrupt intent while database recovery backs off."""

import asyncio
import time
from weakref import WeakKeyDictionary

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.db import has_pending_interrupt
from shared.log import logger
from shared.runtime_incarnation import RuntimeIncarnation

_POLL_INTERVAL_SECONDS = 2.0
_QUERY_TIMEOUT_SECONDS = 5.0
_PEEK_LOCKS: WeakKeyDictionary[AsyncConnectionPool, asyncio.Lock] = WeakKeyDictionary()


class RecoveryInterrupt:
    """Shorten one retry wait when external interrupt intent becomes readable.

    Observation never claims a command, cancels a write, or certifies a paused
    checkpoint. The original task still owns repair and must reach the normal
    claim boundary before control is applied. A repair in flight remains bounded
    by its own deadline; observation happens between attempts, during backoff.

    Poll inline on the reserved control pool: no background task or outstanding
    query can outlive this wait. At most one optional peek borrows from each pool;
    other observers skip that poll instead of competing with ownership/lifecycle
    operations. Once observed, leave subsequent backoffs intact so a persistent
    failure plus a pending cancel cannot create a busy retry loop.
    """

    def __init__(self, pool: AsyncConnectionPool, incarnation: RuntimeIncarnation) -> None:
        self._pool = pool
        self._incarnation = incarnation
        self._observed = False
        lock = _PEEK_LOCKS.get(pool)
        if lock is None:
            lock = asyncio.Lock()
            _PEEK_LOCKS[pool] = lock
        self._peek_lock = lock

    async def wait_backoff(self, delay: float) -> None:
        """Spend the existing backoff budget checking for external control."""
        if self._observed:
            await asyncio.sleep(delay)
            return
        deadline = time.monotonic() + delay
        while (remaining := deadline - time.monotonic()) > 0:
            pending = False
            # The host has one event loop. A free asyncio.Lock acquires without
            # suspending, so this check-and-acquire never queues behind a peek.
            if not self._peek_lock.locked():
                try:
                    async with (
                        self._peek_lock,
                        asyncio.timeout(min(_QUERY_TIMEOUT_SECONDS, remaining)),
                    ):
                        pending = await has_pending_interrupt(
                            self._pool, self._incarnation.agent_id
                        )
                except (psycopg.OperationalError, PoolTimeout, TimeoutError):
                    # This optional read cannot prevent the next repair attempt.
                    # The repair path owns database failure reporting and retries.
                    pending = False
            if pending:
                self._observed = True
                logger.info(
                    "external interrupt observed during database recovery; "
                    "retrying now, durable repair must finish before claim",
                    agent_id=self._incarnation.agent_id,
                    generation=str(self._incarnation.generation),
                    owner=str(self._incarnation.owner),
                )
                return
            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, remaining))
