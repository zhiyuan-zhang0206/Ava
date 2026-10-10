"""Checkpoint cursors own a pool lease instead of a host-wide saver lock."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from psycopg import AsyncConnection, AsyncCursor
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from base.agents.history.checkpoint_postgres_walks import HistoryAsyncPostgresSaver


class MissingCheckpointParentError(RuntimeError):
    """A hosted checkpoint write names a parent absent from its exact thread/namespace."""


class PooledPostgresSaver(HistoryAsyncPostgresSaver):
    """One logical saver, with independent connections for concurrent operations.

    LangGraph's pooled saver takes its instance lock before borrowing a
    connection, serializing unrelated agents. Lease the connection first and
    delegate its cursor to a short-lived upstream saver: the lock now belongs
    to that exclusive lease. Upstream still owns pipeline synchronization,
    transaction fallback, and cursor cleanup on errors or cancellation.

    Checkpoint writes also validate their exact parent on the same lease and
    transaction as the upstream blob/checkpoint inserts. The host's N-step
    wrapper first reparents coalesced saves and serializes each thread's writes
    and flushes. A missing parent is a failed save, never a dangling successor.
    """

    def __init__(
        self,
        conn: AsyncConnectionPool[AsyncConnection[DictRow]],
        *,
        serde: SerializerProtocol | None = None,
    ) -> None:
        super().__init__(conn=conn, serde=serde)
        self._pool = conn

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Commit one checkpoint only after its named parent exists.

        The row lock prevents deletion between this check and commit; it is not
        a foreign key or a guarantee against later retention. Upstream owns
        serialization and inserts, with all blob/checkpoint writes in this
        transaction. First checkpoints may have no parent.
        """
        configurable = config.get("configurable", {})
        parent = configurable.get("checkpoint_id")
        async with self._pool.connection() as conn, conn.transaction():
            if parent is not None:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT 1 FROM checkpoints WHERE thread_id=%s "
                        "AND checkpoint_ns=%s AND checkpoint_id=%s FOR KEY SHARE",
                        (configurable["thread_id"], configurable["checkpoint_ns"], parent),
                    )
                    if await cur.fetchone() is None:
                        raise MissingCheckpointParentError(
                            f"checkpoint parent is missing: thread={configurable['thread_id']} "
                            f"namespace={configurable['checkpoint_ns']!r} parent={parent}"
                        )
            lease = AsyncPostgresSaver(conn=conn, serde=self.serde)
            return await lease.aput(config, checkpoint, metadata, new_versions)

    @asynccontextmanager
    async def _cursor(self, *, pipeline: bool = False) -> AsyncGenerator[AsyncCursor[DictRow]]:
        async with self._pool.connection() as conn:
            lease = AsyncPostgresSaver(conn=conn, serde=self.serde)
            async with lease._cursor(pipeline=pipeline) as cur:
                yield cur
