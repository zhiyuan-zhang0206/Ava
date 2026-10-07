"""Durable im-bridge cursors in Postgres (`im_bridge_cursors`).

Two positions that used to live in process memory, so a restart lost them: the
push acceptance watermark and Feishu's inbound poll cursor. TimelineOutboxStore
owns live push acceptance and chat selection in the same cursor row; this
compatibility owner reads positions and seeds legacy fixtures. The daemon owns
the pool. Feishu's independent inbound poll contract remains unchanged.

Methods are synchronous (psycopg pool): async callers run them in a thread.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, LiteralString, NamedTuple


class PushWatermark(NamedTuple):
    """One chat's push position: the newest item durably accepted for delivery.

    ``created_at`` (ISO-8601, exactly as the timeline stamps items) is the
    primary key — monotone, immune to the compact that renumbers a session's
    item ids (task #4933). ``item_id`` breaks ties between the blocks of one
    message. ``created_at`` is None on rows written before the column existed
    and on items carrying no stamp: those compare by item_id alone — the
    previous ordering semantics. An unqualified legacy source cannot advance it.
    """

    created_at: str | None
    item_id: str


class CursorStore:
    def __init__(self, db_pool: Any = None) -> None:
        self._pool = db_pool

    def load_push(self) -> dict[tuple[str, str, int], PushWatermark]:
        """{(channel, chat_id, agent_id): newest accepted item}, one per chat."""

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


def parse_stamp(raw: object) -> datetime | None:
    """Parse an ISO-8601 created_at to an aware datetime (naive reads as UTC);
    None for absent or unparseable values — a legacy item or row, which
    compares by item_id alone."""

    if not isinstance(raw, str) or not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def watermark_of(item: dict[str, Any]) -> PushWatermark:
    """The push position one timeline item stands for: its created_at when
    stamped (stored verbatim), its item_id always."""

    raw = item.get("created_at")
    stamped = raw if isinstance(raw, str) and parse_stamp(raw) is not None else None
    return PushWatermark(created_at=stamped, item_id=str(item["item_id"]))


def is_after_watermark(item: dict[str, Any], watermark: PushWatermark) -> bool:
    """Whether *item* sits past *watermark* on the chat's push position.

    created_at is the primary key: monotone across the session's whole life,
    while item_id (f"{msg_idx}.{block_idx}") is a POSITION that a compact
    renumbers — the id-only comparison froze both affected pushes on
    2026-10-03 (#4932/#4933). Equal stamps (the blocks of one message share
    one) fall through to the numeric item_id order; a stamp missing or
    unreadable on either side also falls back to item_id alone — the
    legacy ordering only. Positional rollbacks do not advance without accepted intents."""

    item_stamp = parse_stamp(item.get("created_at"))
    watermark_stamp = parse_stamp(watermark.created_at)
    if item_stamp is not None and watermark_stamp is not None and item_stamp != watermark_stamp:
        return item_stamp > watermark_stamp
    return item_key(str(item["item_id"])) > item_key(watermark.item_id)


def item_key(item_id: str) -> tuple[int, int]:
    try:
        msg_idx, block_idx = item_id.split(".", 1)
        return (int(msg_idx), int(block_idx or 0))
    except ValueError:
        return (0, 0)
