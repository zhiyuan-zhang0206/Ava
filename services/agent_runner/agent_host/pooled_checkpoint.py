"""Checkpoint cursors own a pool lease instead of a host-wide saver lock."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from psycopg import AsyncConnection, AsyncCursor
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool


class PooledPostgresSaver(AsyncPostgresSaver):
    """One logical saver, with independent connections for concurrent operations.

    LangGraph's pooled saver takes its instance lock before borrowing a
    connection, serializing unrelated agents. Lease the connection first and
    delegate its cursor to a short-lived upstream saver: the lock now belongs
    to that exclusive lease. Upstream still owns pipeline synchronization,
    transaction fallback, and cursor cleanup on errors or cancellation.

    This changes only cursor ownership. The host's N-step wrapper continues to
    serialize writes and flushes for each thread on this logical saver.
    """

    def __init__(
        self,
        conn: AsyncConnectionPool[AsyncConnection[DictRow]],
        *,
        serde: SerializerProtocol | None = None,
    ) -> None:
        super().__init__(conn=conn, serde=serde)
        self._pool = conn

    @asynccontextmanager
    async def _cursor(self, *, pipeline: bool = False) -> AsyncGenerator[AsyncCursor[DictRow]]:
        async with self._pool.connection() as conn:
            lease = AsyncPostgresSaver(conn=conn, serde=self.serde)
            async with lease._cursor(pipeline=pipeline) as cur:
                yield cur
