"""Expand a checkpoint session marker with a bounded page of permanent messages."""

from typing import Any

from psycopg.rows import dict_row

from shared.agents.history.timeline import TimelineItem
from shared.agents.impersonation.impersonation_history import ImpersonationMetadata
from shared.db import connect
from shared.inbound_images import inbound_image_urls


def hydrate(
    items: list[TimelineItem],
    agent_id: int,
    *,
    limit: int,
    before: str | None = None,
) -> list[TimelineItem]:
    """Use the marker's message position and each immutable entry's sequence.

    Extra blocks share the existing numeric cursor grammar. Only limit+1
    message rows are loaded across the relevant sessions, even for an arbitrarily
    long takeover; scroll-back selects the preceding sequences from PostgreSQL.
    The same operation handles live and compacted checkpoint segments.
    """
    anchors: dict[int, TimelineItem] = {}
    upper_session: int | None = None
    upper_seq: int | None = None
    cursor_position = _position(before) if before is not None else None
    for item in items:
        info = item.impersonation
        if info is None or info.seq is not None:
            continue
        if info.agent_id != agent_id:
            raise ValueError("Impersonation timeline marker belongs to another agent")
        position = _position(item.item_id)
        if cursor_position is not None and position[0] > cursor_position[0]:
            continue
        anchors[info.session_id] = item
        if cursor_position is not None and position[0] == cursor_position[0]:
            upper_session, upper_seq = info.session_id, cursor_position[1] - 1
    if not anchors:
        return items
    with connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT p.session_id,e.seq,e.created_at,e.payload FROM agent_impersonations p "
            "JOIN agent_impersonation_entries e ON e.lease_id=p.id "
            "WHERE p.agent_id=%s AND p.session_id=ANY(%s) AND e.kind='message' "
            "AND (%s::bigint IS NULL OR p.session_id<>%s OR e.seq<=%s) "
            "ORDER BY p.session_id DESC,e.seq DESC LIMIT %s",
            (agent_id, list(anchors), upper_session, upper_session, upper_seq, limit + 1),
        )
        rows = cur.fetchall()
    rendered = [_message(anchors[row["session_id"]], row) for row in reversed(rows)]
    return sorted([*items, *rendered], key=lambda item: _position(item.item_id))


def _position(item_id: str) -> tuple[int, int]:
    message, block = item_id.split(".")[-2:]
    return int(message), int(block)


def _message(anchor: TimelineItem, row: dict[str, Any]) -> TimelineItem:
    payload = row["payload"]
    info = anchor.impersonation
    assert info is not None  # noqa: S101 — selected marker
    outgoing = payload["direction"] == "out"
    prefix = anchor.item_id.rsplit(".", 1)[0]
    return TimelineItem(
        item_id=f"{prefix}.{row['seq'] + 1}",
        kind="agent_chat" if outgoing else "inbound_chat",
        source=payload["source"],
        payload=payload["content"],
        images=None if outgoing else inbound_image_urls(info.agent_id, payload["payload"]),
        created_at=row["created_at"].isoformat(),
        inbound_id=None if outgoing else payload["inbound_id"],
        impersonation=ImpersonationMetadata.model_validate(
            {
                **info.model_dump(),
                "anchor_item_id": anchor.item_id,
                "seq": row["seq"],
            }
        ),
    )
