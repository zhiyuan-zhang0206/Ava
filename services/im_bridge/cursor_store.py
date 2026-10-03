"""Durable im-bridge cursors in Postgres (`im_bridge_cursors`).

Two positions that used to live in process memory, so a restart lost them: the
push watermark (newest agent item already pushed to a chat; the SSE feed is a
live tail, so nothing re-delivers what was missed while the bridge was down)
and Feishu's inbound poll cursor (the platform keeps no offset for polling).
The daemon owns the pool and always passes it; a core built without one (unit
tests) keeps both in memory only.

Methods are synchronous (psycopg pool): async callers run them in a thread.
"""

from __future__ import annotations

from typing import Any, LiteralString, NamedTuple


class PushWatermark(NamedTuple):
    """One chat's push position: the newest agent item already sent to it.

    ``created_at`` (ISO-8601, exactly as the timeline stamps items) is the
    primary key — monotone, immune to the compact that renumbers a session's
    item ids (task #4933). ``item_id`` breaks ties between the blocks of one
    message. ``created_at`` is None on rows written before the column existed
    and on items carrying no stamp: those compare by item_id alone — the
    pre-#4933 semantics, recovered by the rollback reset in core.
    """

    created_at: str | None
    item_id: str


class CursorStore:
    def __init__(self, db_pool: Any = None) -> None:
        self._pool = db_pool

    def load_push(self) -> dict[tuple[str, str, int], PushWatermark]:
        """{(channel, chat_id, agent_id): newest pushed item}, one per chat."""

        if self._pool is None:
            return {}
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT channel, chat_id, push_agent_id, push_item_id, push_created_at "
                "FROM im_bridge_cursors WHERE push_item_id IS NOT NULL"
            )
            return {
                (ch, chat, int(agent)): PushWatermark(created_at=stamp, item_id=item)
                for ch, chat, agent, item, stamp in cur.fetchall()
            }

    def save_push(
        self, channel: str, chat_id: str, agent_id: int, watermark: PushWatermark
    ) -> None:
        self._upsert(
            "INSERT INTO im_bridge_cursors "
            "(channel, chat_id, push_agent_id, push_item_id, push_created_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (channel, chat_id) DO UPDATE SET "
            "push_agent_id = EXCLUDED.push_agent_id, push_item_id = EXCLUDED.push_item_id, "
            "push_created_at = EXCLUDED.push_created_at, updated_at = now()",
            (channel, chat_id, agent_id, watermark.item_id, watermark.created_at),
        )

    def load_poll(self, channel: str) -> dict[str, tuple[str, int]]:
        """{conversation id: (newest handled message id or '', its create time in ms)}."""

        if self._pool is None:
            return {}
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT chat_id, poll_message_id, poll_create_ms FROM im_bridge_cursors "
                "WHERE channel = %s AND poll_create_ms IS NOT NULL",
                (channel,),
            )
            return {chat: (message_id or "", int(ms)) for chat, message_id, ms in cur.fetchall()}

    def save_poll(self, channel: str, chat_id: str, message_id: str, create_ms: int) -> None:
        self._upsert(
            "INSERT INTO im_bridge_cursors (channel, chat_id, poll_message_id, poll_create_ms) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (channel, chat_id) DO UPDATE SET "
            "poll_message_id = EXCLUDED.poll_message_id, poll_create_ms = EXCLUDED.poll_create_ms, "
            "updated_at = now()",
            (channel, chat_id, message_id or None, create_ms),
        )

    def _upsert(self, statement: LiteralString, params: tuple[Any, ...]) -> None:
        if self._pool is None:
            return
        from base.db.transaction import write_transaction

        with write_transaction(self._pool) as conn, conn.cursor() as cur:
            cur.execute(statement, params)
