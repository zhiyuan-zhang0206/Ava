"""Explicit database transaction postures."""

from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager

import psycopg
from psycopg_pool import AsyncConnectionPool, ConnectionPool


@contextmanager
def write_transaction(
    pool: ConnectionPool | None = None, *, timeout: float | None = None
) -> Generator[psycopg.Connection, None, None]:
    """Open one transaction explicitly allowed to write.

    PgBouncer can assign a client a backend whose session state another client
    changed to default read-only. Declare this transaction writable as its first
    statement before DML, pinning the borrowed backend until the context commits
    or rolls back. Pool connections must not use autocommit.
    """
    if pool is None:
        from shared.db import connect

        connection = connect()
    else:
        connection = pool.connection(timeout=timeout)
    with connection as conn:
        conn.execute("SET TRANSACTION READ WRITE")
        yield conn


@asynccontextmanager
async def async_write_transaction(
    pool: AsyncConnectionPool, *, timeout: float | None = None
) -> AsyncGenerator[psycopg.AsyncConnection, None]:
    """Borrow one async connection for an explicitly writable transaction.

    PgBouncer can reuse a backend whose prior client left its default transaction
    read-only. Async pool connections use autocommit, so open a transaction before
    declaring it writable and yielding it for DML.
    """
    async with pool.connection(timeout=timeout) as conn, conn.transaction():
        await conn.execute("SET TRANSACTION READ WRITE")
        yield conn
