"""Checkpoint cursors own a pool lease instead of a host-wide saver lock.

The saver also carries the serialized-blob size guard: a checkpoint write
whose serialized blob exceeds ``AVA_CHECKPOINT_MAX_BLOB_BYTES`` is refused —
fast and loudly, before any SQL runs — instead of stalling on the database
statement timeout with a multi-megabyte rewrite.
"""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.base import ChannelVersions
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from psycopg import AsyncConnection, AsyncCursor
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from shared.config import settings


class CheckpointBlobTooLargeError(RuntimeError):
    """A checkpoint write was refused because one serialized blob is over the limit."""


class PooledPostgresSaver(AsyncPostgresSaver):
    """One logical saver, with independent connections for concurrent operations.

    LangGraph's pooled saver takes its instance lock before borrowing a
    connection, serializing unrelated agents. Lease the connection first and
    delegate its cursor to a short-lived upstream saver: the lock now belongs
    to that exclusive lease. Upstream still owns pipeline synchronization,
    transaction fallback, and cursor cleanup on errors or cancellation.

    This changes only cursor ownership. The host's N-step wrapper continues to
    serialize writes and flushes for each thread on this logical saver.

    ``_dump_blobs`` / ``_dump_writes`` add the blob size guard: rows are
    serialized by upstream exactly as before, then any single row's blob over
    the configured limit is refused with ``CheckpointBlobTooLargeError``
    before the write is sent — nothing is trimmed or silently dropped. An
    existing thread that already holds an oversized blob keeps failing this
    way until its content shrinks: the guard converts the stall into a fast,
    explicit failure; it does not repair old data.
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

    def _dump_blobs(
        self,
        thread_id: str,
        checkpoint_ns: str,
        values: dict[str, Any],
        versions: ChannelVersions,
    ) -> list[tuple[str, str, str, str, str, bytes | None]]:
        rows = super()._dump_blobs(thread_id, checkpoint_ns, values, versions)
        # Row layout: thread_id, checkpoint_ns, channel, version, type, blob.
        for row in rows:
            self._refuse_oversized_blob(thread_id, row[2], row[-1])
        return rows

    def _dump_writes(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        task_id: str,
        task_path: str,
        writes: Sequence[tuple[str, Any]],
    ) -> list[tuple[str, str, str, str, str, int, str, str, bytes]]:
        rows = super()._dump_writes(
            thread_id, checkpoint_ns, checkpoint_id, task_id, task_path, writes
        )
        # Row layout: thread_id, checkpoint_ns, checkpoint_id, task_id, task_path, idx, channel, type, blob.
        for row in rows:
            self._refuse_oversized_blob(thread_id, row[6], row[-1])
        return rows

    def _refuse_oversized_blob(self, thread_id: str, channel: object, blob: bytes | None) -> None:
        """Refuse one serialized blob over the configured limit before any SQL runs.

        The pathological case this guards: multi-megabyte inline content (e.g.
        base64 images in the messages channel) is rewritten in full on every
        checkpoint, and a cross-network write of such a blob stalls at the
        database statement timeout — the write is retried with the same
        oversized payload and the turn wedges. Failing here converts that into
        an immediate, explicit error naming the channel, the size and the
        limit. Nothing is trimmed or silently dropped: the write does not
        happen at all.

        ``thread_id`` is the agent id in this deployment; an entry in
        ``AVA_CHECKPOINT_MAX_BLOB_BYTES_OVERRIDES`` replaces the base limit for
        that thread (an agent whose history already carries an oversized blob
        can keep writing while the storage fix lands).
        """
        if blob is None:
            return
        override = settings.agent.checkpoint_max_blob_bytes_overrides.get(thread_id)
        limit = settings.agent.checkpoint_max_blob_bytes if override is None else override
        if len(blob) <= limit:
            return
        knob = (
            "AVA_CHECKPOINT_MAX_BLOB_BYTES"
            if override is None
            else "AVA_CHECKPOINT_MAX_BLOB_BYTES_OVERRIDES"
        )
        mib = 1024 * 1024
        raise CheckpointBlobTooLargeError(
            f"checkpoint write refused: channel {channel!r} serialized to "
            f"{len(blob) / mib:.1f} MiB, over the {limit / mib:.0f} MiB blob limit "
            f"({knob}); split the content or downscale/reduce its attached images "
            "before the next write — nothing was written"
        )
