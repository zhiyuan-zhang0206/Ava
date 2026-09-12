"""Database pool construction for the hosted agent runner."""

from __future__ import annotations

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.db import LoggingConnectionPool
from shared.config import settings
from shared.db import PG_KEEPALIVE_KWARGS, _restore_pooled_session_async


def build_shared_pool(dsn: str) -> AsyncConnectionPool[psycopg.AsyncConnection]:
    """The host's turn/checkpoint pool, independent of active agent count.

    Agents waiting on models or tools need no dedicated connection. The client
    budget covers short database borrows and is shared by all active agents.
    `autocommit=True` +
    `prepare_threshold=None` satisfy the saver and pooler: the saver expects
    autocommit, and never preparing
    is what keeps borrows safe across PgBouncer's transaction pooling.
    `min_size=0` keeps no warm idle connection: the pool opens connections on
    demand, so the pre-stop release (`/release-db-pools`) drains it to zero and
    the first borrow after resume reconnects lazily.
    """
    return LoggingConnectionPool[psycopg.AsyncConnection](
        dsn,
        pool_name="agent-host",
        min_size=0,
        max_size=settings.daemon.host_db_pool_max_size,
        kwargs={"autocommit": True, "prepare_threshold": None, **PG_KEEPALIVE_KWARGS},
        check=_restore_pooled_session_async,
        timeout=settings.agent.db_pool_acquire_timeout_seconds,
        open=False,
    )


def build_control_pool(dsn: str) -> AsyncConnectionPool[psycopg.AsyncConnection]:
    """Reserved capacity for host ownership, recovery, and durable scans.

    PgBouncer remains the downstream server-connection multiplexer. This
    separate client pool cannot be consumed by turn or checkpoint borrowers.
    Both pools use the same database role, so backend capacity and queueing
    remain shared in PgBouncer; this is not a reserved PostgreSQL server pool.
    `min_size=0` mirrors the shared pool: an idle host holds no client
    connection.
    """
    return LoggingConnectionPool[psycopg.AsyncConnection](
        dsn,
        pool_name="agent-host-control",
        min_size=0,
        max_size=settings.daemon.host_control_pool_max_size,
        kwargs={"autocommit": True, "prepare_threshold": None, **PG_KEEPALIVE_KWARGS},
        check=_restore_pooled_session_async,
        timeout=settings.agent.db_pool_acquire_timeout_seconds,
        open=False,
    )
