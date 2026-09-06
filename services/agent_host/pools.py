"""Database pool construction for the hosted agent runner."""

from __future__ import annotations

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.db import LoggingConnectionPool
from shared.config import settings
from shared.db import PG_KEEPALIVE_KWARGS, _restore_pooled_session_async

# Spare workload connections above the concurrent-turn bound. Every running
# turn may hold one for checkpoint writes or kernel SQL; the spares cover
# checkpoint/background overlap. Lifecycle and durable scans have a separate
# control pool and cannot be starved by these borrowers.
_POOL_HEADROOM = 4
_CONTROL_POOL_SIZE = 4


def build_shared_pool(dsn: str) -> AsyncConnectionPool[psycopg.AsyncConnection]:
    """The host's turn/checkpoint pool, sized from the concurrent-turn bound.

    `max_size` is the bound plus headroom rather than a round number, so the
    sizing states its reason (see `_POOL_HEADROOM`). `autocommit=True` +
    `prepare_threshold=None` satisfy the saver and pooler: the saver expects
    autocommit, and never preparing
    is what keeps borrows safe across PgBouncer's transaction pooling.
    """
    return LoggingConnectionPool[psycopg.AsyncConnection](
        dsn,
        pool_name="agent-host",
        min_size=1,
        max_size=settings.daemon.host_max_concurrent_turns + _POOL_HEADROOM,
        kwargs={"autocommit": True, "prepare_threshold": None, **PG_KEEPALIVE_KWARGS},
        check=_restore_pooled_session_async,
        timeout=settings.agent.db_pool_acquire_timeout_seconds,
        open=False,
    )


def build_control_pool(dsn: str) -> AsyncConnectionPool[psycopg.AsyncConnection]:
    """Reserved capacity for host ownership, recovery, and durable scans.

    PgBouncer remains the downstream server-connection multiplexer. This
    small client pool is a correctness boundary inside agent-host: turn or
    checkpoint borrowers cannot consume it, so saturation cannot hide pending
    work or strand lifecycle settlement.
    """
    return LoggingConnectionPool[psycopg.AsyncConnection](
        dsn,
        pool_name="agent-host-control",
        min_size=1,
        max_size=_CONTROL_POOL_SIZE,
        kwargs={"autocommit": True, "prepare_threshold": None, **PG_KEEPALIVE_KWARGS},
        check=_restore_pooled_session_async,
        timeout=settings.agent.db_pool_acquire_timeout_seconds,
        open=False,
    )
