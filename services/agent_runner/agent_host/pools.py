"""Database pool construction for the hosted agent runner."""

from __future__ import annotations

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.db import LoggingConnectionPool
from base.config import settings
from base.db import Database


def build_shared_pool(db: Database) -> AsyncConnectionPool[psycopg.AsyncConnection]:
    """The host's turn/checkpoint pool, independent of active agent count.

    Agents waiting on models or tools need no dedicated connection. The client
    budget covers short database borrows and is shared by all active agents.
    The transport posture (autocommit for the saver, no prepared statements
    across PgBouncer backends, the per-borrow session scrub) is
    `Database.async_pool`'s. `min_size=0` keeps no warm idle connection: the
    pool opens connections on demand and an idle one ages out.
    """
    return db.async_pool(
        LoggingConnectionPool[psycopg.AsyncConnection],
        pool_name="agent-host",
        min_size=0,
        max_size=settings.daemon.host_db_pool_max_size,
        timeout=settings.agent.db_pool_acquire_timeout_seconds,
    )


def build_control_pool(db: Database) -> AsyncConnectionPool[psycopg.AsyncConnection]:
    """Reserved capacity for host ownership, recovery, and durable scans.

    PgBouncer remains the downstream server-connection multiplexer. This
    separate client pool cannot be consumed by turn or checkpoint borrowers.
    Both pools use the same database role, so backend capacity and queueing
    remain shared in PgBouncer; this is not a reserved PostgreSQL server pool.
    `min_size=0` mirrors the shared pool: an idle host holds no client
    connection.
    """
    return db.async_pool(
        LoggingConnectionPool[psycopg.AsyncConnection],
        pool_name="agent-host-control",
        min_size=0,
        max_size=settings.daemon.host_control_pool_max_size,
        timeout=settings.agent.db_pool_acquire_timeout_seconds,
    )
