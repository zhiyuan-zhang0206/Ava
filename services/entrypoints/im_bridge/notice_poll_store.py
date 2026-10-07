"""Normal notice decisions share the existing IM outbound intent transaction."""

from typing import Any

from psycopg.types.json import Jsonb

from base.db.transaction import write_transaction
from services.entrypoints.im_bridge.outbound_store import IMOutboxStore
from services.entrypoints.im_bridge.outbound_types import (
    NoticePollDecision,
    NoticePollImportReason,
    NoticePollReceipt,
    OutboundIntent,
    OutboundSourceKind,
)


class NoticePollStore:
    """Notice-specific cutover and receipt owner; no separate dispatch queue."""

    def __init__(self, outbound: IMOutboxStore) -> None:
        self.outbound = outbound

    def initialize_notice_poll(
        self, legacy_cursor: int | None
    ) -> tuple[int, int, NoticePollImportReason]:
        """Import a fixed cutover once; a diagnostic high ID never gates reads."""
        if legacy_cursor is not None and (type(legacy_cursor) is not int or legacy_cursor < 0):
            raise ValueError("legacy notice cursor must be a non-negative integer or unknown")
        with write_transaction(self.outbound._pool()) as conn:
            maximum = conn.execute("SELECT COALESCE(max(id), 0) FROM agent_notices").fetchone()
            if maximum is None:
                raise RuntimeError("notice cutover aggregate returned no row")
            if legacy_cursor is not None:
                floor, reason = legacy_cursor, NoticePollImportReason.LEGACY_CURSOR
            elif maximum[0] == 0:
                floor, reason = 0, NoticePollImportReason.NO_HISTORY
            else:
                floor, reason = int(maximum[0]), NoticePollImportReason.LEGACY_HISTORY_UNKNOWN
            conn.execute(
                "INSERT INTO im_bridge_notice_poll_state (legacy_floor,import_reason,accepted_notice_id) "
                "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (floor, reason.value, floor),
            )
            row = conn.execute(
                "SELECT legacy_floor,accepted_notice_id,import_reason FROM im_bridge_notice_poll_state "
                "WHERE singleton FOR UPDATE"
            ).fetchone()
            if row is None:
                raise RuntimeError("notice cutover disappeared within its initialization")
            return int(row[0]), int(row[1]), NoticePollImportReason(row[2])

    def accept_notice(
        self,
        notice_id: int,
        snapshot: dict[str, Any],
        intent: OutboundIntent | None,
        *,
        filtered: bool,
    ) -> NoticePollReceipt:
        """Freeze one normal-poll decision with its shared outbound intent.

        Source receipt lookup comes before mutable target/render comparison:
        an accepted notice cannot be retargeted by a later owner/config change.
        The singleton orders concurrent normal pollers, not provider sends.
        """
        self._validate_notice(notice_id, intent, filtered=filtered)
        with write_transaction(self.outbound._pool()) as conn:
            state = conn.execute(
                "SELECT legacy_floor FROM im_bridge_notice_poll_state WHERE singleton FOR UPDATE"
            ).fetchone()
            if state is None:
                raise RuntimeError("normal notice poll must initialize its cutover first")
            if notice_id <= state[0]:
                raise ValueError("notice belongs to the retained legacy cutover range")
            previous = conn.execute(
                "SELECT decision,intent_ids FROM im_bridge_notice_acceptances WHERE notice_id=%s",
                (notice_id,),
            ).fetchone()
            if previous is not None:
                return NoticePollReceipt(NoticePollDecision(previous[0]), tuple(previous[1]))
            decision = NoticePollDecision.FILTERED if filtered else NoticePollDecision.QUEUED
            ids = () if filtered else (self.outbound.insert_intent(conn, intent),)
            request = {
                "source": snapshot,
                "intent": intent.model_dump(mode="json") if intent else None,
            }
            conn.execute(
                "INSERT INTO im_bridge_notice_acceptances (notice_id,decision,request,intent_ids) "
                "VALUES (%s,%s,%s,%s)",
                (notice_id, decision.value, Jsonb(request), list(ids)),
            )
            conn.execute(
                "UPDATE im_bridge_notice_poll_state SET accepted_notice_id=GREATEST(accepted_notice_id,%s), "
                "updated_at=now() WHERE singleton",
                (notice_id,),
            )
            return NoticePollReceipt(decision, ids)

    @staticmethod
    def _validate_notice(notice_id: int, intent: OutboundIntent | None, *, filtered: bool) -> None:
        if type(notice_id) is not int or notice_id <= 0:
            raise ValueError("normal notice source requires a positive global notice ID")
        if filtered:
            if intent is not None:
                raise ValueError("filtered notices cannot enqueue an intent")
            return
        if intent is None:
            raise ValueError("normal notice acceptance requires its immutable Telegram intent")
        identity = (
            intent.channel,
            intent.source.kind,
            intent.source.identity,
            intent.source.block_idx,
            intent.replay_id,
        )
        if identity != ("telegram", OutboundSourceKind.NOTICE, str(notice_id), 0, ""):
            raise ValueError("normal notice acceptance requires its immutable Telegram intent")
