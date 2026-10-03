"""Explicit database transaction postures."""

from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from typing import Any, TypeVar

import psycopg
from psycopg_pool import AsyncConnectionPool, ConnectionPool

_CT = TypeVar("_CT", bound=psycopg.AsyncConnection[Any])


@contextmanager
def write_transaction(
    pool: ConnectionPool, *, timeout: float | None = None
) -> Generator[psycopg.Connection, None, None]:
    """Open one transaction explicitly allowed to write, on a connection of `pool`.

    The session may default to read-only without this client asking: a pooler
    that does not track `default_transaction_read_only` per client (PgBouncer
    before 1.26; a remote-managed data plane's pooler is not ours to pin) hands
    over what another client SET, and a database or role default survives the
    pooled session scrub. Declare this transaction writable as its first
    statement before DML, pinning the borrowed backend until the context
    commits or rolls back. Pool connections must not use autocommit.
    """
    with pool.connection(timeout=timeout) as conn:
        conn.execute("SET TRANSACTION READ WRITE")
        yield conn


@asynccontextmanager
async def async_write_transaction(  # noqa: UP047 - preserve the pool connection subtype.
    pool: AsyncConnectionPool[_CT], *, timeout: float | None = None
) -> AsyncGenerator[_CT, None]:
    """Borrow one async connection for an explicitly writable transaction.

    The session may default to read-only without this client asking (see
    `write_transaction`). Async pool connections use autocommit, so open a
    transaction before declaring it writable and yielding it for DML.
    """
    async with pool.connection(timeout=timeout) as conn, conn.transaction():
        await conn.execute("SET TRANSACTION READ WRITE")
        yield conn
