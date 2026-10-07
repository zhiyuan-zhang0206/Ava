"""IM intents share atomic acceptance with their timeline or notice producer."""

from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.db.transaction import write_transaction
from base.telemetry.alerts.native import stamp_native_sent
from services.entrypoints.im_bridge.cursor_store import (
    PushWatermark,
    is_after_watermark,
    watermark_of,
)
from services.entrypoints.im_bridge.outbound_types import (
    OutboundAccountMismatchError,
    OutboundIdentityConflictError,
    OutboundIntent,
    OutboundStatus,
    TimelineAcceptance,
    TimelineCandidate,
)


class IMOutboxStore:
    def __init__(self, pool: ConnectionPool | None) -> None:
        self.pool = pool

    def _pool(self) -> ConnectionPool:
        if self.pool is None:
            raise RuntimeError("timeline outbound acceptance requires a database pool")
        return self.pool

    def accept(
        self,
        channel: str,
        account_id: str,
        chat_id: str,
        agent_id: int,
        candidates: Sequence[TimelineCandidate],
        *,
        replay_id: str = "",
        switch_arg: str = "",
        guard: Callable[[Connection], None] | None = None,
    ) -> TimelineAcceptance:
        self._validate_candidates(channel, account_id, chat_id, agent_id, candidates, replay_id)
        with write_transaction(self._pool()) as conn:
            if guard is not None:
                guard(conn)
            saved_account, saved_agent, watermark, initialized = self._lock_cursor(
                conn,
                channel,
                chat_id,
                initial_agent=agent_id if not replay_id else None,
                initialize=not bool(replay_id),
            )
            if replay_id:
                previous = self._replay_receipt(
                    conn,
                    channel,
                    account_id,
                    chat_id,
                    replay_id,
                    switch_arg,
                    saved_agent,
                    expected_agent=agent_id,
                )
                if previous is not None:
                    return previous
            else:
                held = self._regular_hold(
                    conn,
                    (channel, account_id, chat_id),
                    agent_id,
                    saved_account,
                    saved_agent,
                    watermark,
                    initialized=initialized,
                )
                if held is not None:
                    return held
            fresh = (
                list(candidates)
                if replay_id
                else [
                    candidate
                    for candidate in candidates
                    if self._fresh(candidate, agent_id, saved_agent, watermark)
                ]
            )
            accepted = self._qualified_prefix(fresh)
            if replay_id and len(accepted) != len(fresh):
                return TimelineAcceptance((), None, blocked=True, selected_agent_id=saved_agent)
            ids = tuple(self.insert_intent(conn, candidate.intent) for candidate in accepted)
            position = self._position(accepted, agent_id, saved_agent, watermark, replay_id)
            self._record_acceptance(
                conn,
                (channel, account_id, chat_id),
                agent_id,
                ids,
                position,
                saved_account,
                accepted=bool(accepted),
                replay=replay_id,
                switch_arg=switch_arg,
            )
            return TimelineAcceptance(
                ids,
                position,
                blocked=len(accepted) != len(fresh),
                selected_agent_id=agent_id if accepted or replay_id else saved_agent,
            )

    @staticmethod
    def _validate_candidates(
        channel: str,
        account: str,
        chat: str,
        agent: int,
        candidates: Sequence[TimelineCandidate],
        replay: str,
    ) -> None:
        for candidate in candidates:
            intent = candidate.intent
            if intent is not None and (
                intent.channel,
                intent.prepared.account_id,
                intent.chat_id,
                intent.agent_id,
                intent.replay_id,
            ) != (channel, account, chat, agent, replay):
                raise ValueError("timeline candidate belongs to another acceptance stream")

    @staticmethod
    def _regular_hold(
        conn: Connection,
        stream: tuple[str, str, str],
        agent: int,
        account: str | None,
        selected: int | None,
        watermark: PushWatermark | None,
        *,
        initialized: bool,
    ) -> TimelineAcceptance | None:
        channel, incoming_account, chat = stream
        if account is not None and account != incoming_account:
            raise OutboundAccountMismatchError("push cursor belongs to another adapter account")
        if watermark is None and not initialized:
            if account is None:
                conn.execute(
                    "UPDATE im_bridge_cursors SET push_account_id=%s WHERE channel=%s AND chat_id=%s",
                    (incoming_account, channel, chat),
                )
            return TimelineAcceptance((), None, blocked=True, selected_agent_id=selected)
        if initialized and selected != agent:
            return TimelineAcceptance((), watermark, blocked=True, selected_agent_id=selected)
        return None

    @staticmethod
    def _fresh(
        candidate: TimelineCandidate,
        agent: int,
        selected: int | None,
        watermark: PushWatermark | None,
    ) -> bool:
        return (
            selected != agent or watermark is None or is_after_watermark(candidate.item, watermark)
        )

    @staticmethod
    def _qualified_prefix(fresh: Sequence[TimelineCandidate]) -> list[TimelineCandidate]:
        accepted: list[TimelineCandidate] = []
        for candidate in fresh:
            if candidate.intent is None:
                break
            accepted.append(candidate)
        return accepted

    @staticmethod
    def _position(
        accepted: Sequence[TimelineCandidate],
        agent: int,
        selected: int | None,
        watermark: PushWatermark | None,
        replay: str,
    ) -> PushWatermark | None:
        if accepted:
            # Replay preserves descending delivery order, with its newest item first.
            return watermark_of(accepted[0 if replay else -1].item)
        return watermark if selected == agent and not replay else None

    def _record_acceptance(
        self,
        conn: Connection,
        stream: tuple[str, str, str],
        agent: int,
        ids: tuple[int, ...],
        position: PushWatermark | None,
        saved_account: str | None,
        *,
        accepted: bool,
        replay: str,
        switch_arg: str,
    ) -> None:
        channel, account, chat = stream
        if accepted or replay:
            self._save_cursor(conn, channel, chat, account, agent, position)
        elif saved_account is None:
            conn.execute(
                "UPDATE im_bridge_cursors SET push_account_id=%s WHERE channel=%s AND chat_id=%s",
                (account, channel, chat),
            )
        if replay:
            conn.execute(
                "INSERT INTO im_bridge_outbound_replays "
                "(channel, account_id, chat_id, replay_id, switch_arg, agent_id, intent_ids, push_item_id, push_created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    channel,
                    account,
                    chat,
                    replay,
                    switch_arg,
                    agent,
                    list(ids),
                    position.item_id if position else None,
                    position.created_at if position else None,
                ),
            )

    @staticmethod
    def _lock_cursor(
        conn: Connection,
        channel: str,
        chat_id: str,
        *,
        initial_agent: int | None = None,
        initialize: bool = False,
    ) -> tuple[Any, Any, PushWatermark | None, bool]:
        conn.execute(
            "INSERT INTO im_bridge_cursors (channel, chat_id, push_agent_id, push_initialized) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (channel, chat_id, initial_agent, initialize),
        )
        row = conn.execute(
            "SELECT push_account_id, push_agent_id, push_created_at, push_item_id, push_initialized "
            "FROM im_bridge_cursors WHERE channel=%s AND chat_id=%s FOR UPDATE",
            (channel, chat_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("outbound cursor or intent disappeared within its transaction")
        return row[0], row[1], PushWatermark(row[2], row[3]) if row[3] is not None else None, row[4]

    @staticmethod
    def _save_cursor(
        conn: Connection,
        channel: str,
        chat_id: str,
        account: str,
        agent: int,
        position: PushWatermark | None,
    ) -> None:
        conn.execute(
            "UPDATE im_bridge_cursors SET push_account_id=%s, push_agent_id=%s, "
            "push_item_id=%s, push_created_at=%s, push_initialized=TRUE, updated_at=now() WHERE channel=%s AND chat_id=%s",
            (
                account,
                agent,
                position.item_id if position else None,
                position.created_at if position else None,
                channel,
                chat_id,
            ),
        )

    @staticmethod
    def _replay_receipt(
        conn: Connection,
        channel: str,
        account: str,
        chat: str,
        replay: str,
        switch_arg: str,
        selected_agent: int | None,
        *,
        expected_agent: int | None = None,
    ) -> TimelineAcceptance | None:
        row = conn.execute(
            "SELECT agent_id, intent_ids, push_created_at, push_item_id, switch_arg FROM im_bridge_outbound_replays "
            "WHERE channel=%s AND account_id=%s AND chat_id=%s AND replay_id=%s",
            (channel, account, chat, replay),
        ).fetchone()
        if row is None:
            return None
        if row[4] != switch_arg or (expected_agent is not None and row[0] != expected_agent):
            raise OutboundIdentityConflictError(
                "switch replay identity already accepted another target"
            )
        return TimelineAcceptance(
            tuple(row[1]),
            PushWatermark(row[2], row[3]) if row[3] else None,
            selected_agent_id=selected_agent,
        )

    @staticmethod
    def insert_intent(conn: Connection, intent: OutboundIntent | None) -> int:
        if intent is None:
            raise ValueError("unqualified timeline source cannot be inserted")
        key = (
            intent.channel,
            intent.prepared.account_id,
            intent.chat_id,
            intent.agent_id,
            intent.source.kind.value,
            intent.source.identity,
            intent.source.block_idx,
            intent.replay_id,
        )
        request = intent.model_dump(mode="json")
        row = conn.execute(
            "INSERT INTO im_bridge_outbound_intents "
            "(channel,account_id,chat_id,agent_id,source_kind,source_id,block_idx,replay_id,request) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id",
            (*key, Jsonb(request)),
        ).fetchone()
        if row is not None:
            return int(row[0])
        row = conn.execute(
            "SELECT id, request FROM im_bridge_outbound_intents WHERE channel=%s AND account_id=%s "
            "AND chat_id=%s AND agent_id IS NOT DISTINCT FROM %s AND source_kind=%s AND source_id=%s AND block_idx=%s AND replay_id=%s",
            key,
        ).fetchone()
        if row is None:
            raise RuntimeError("outbound cursor or intent disappeared within its transaction")
        if row[1] != request:
            raise OutboundIdentityConflictError(
                "timeline source identity has another immutable request"
            )
        return int(row[0])

    def lookup_replay(
        self, channel: str, account: str, chat: str, replay: str, switch_arg: str
    ) -> TimelineAcceptance | None:
        """Recover accepted commands before querying mutable agent status/labels."""
        with write_transaction(self._pool()) as conn:
            row = conn.execute(
                "SELECT push_account_id,push_agent_id FROM im_bridge_cursors WHERE channel=%s AND chat_id=%s FOR UPDATE",
                (channel, chat),
            ).fetchone()
            if row is None:
                return None
            saved_account, selected = row
            previous = self._replay_receipt(
                conn, channel, account, chat, replay, switch_arg, selected
            )
            if previous is not None and saved_account is not None and saved_account != account:
                raise OutboundAccountMismatchError(
                    "accepted replay belongs to a superseded account"
                )
            return previous

    def replay_result(
        self, channel: str, account: str, chat: str, replay: str, switch_arg: str
    ) -> dict[str, object] | None:
        """Return the original switch receipt facts, never a later current selection."""
        with write_transaction(self._pool()) as conn:
            row = conn.execute(
                "SELECT agent_id,intent_ids,switch_arg FROM im_bridge_outbound_replays "
                "WHERE channel=%s AND account_id=%s AND chat_id=%s AND replay_id=%s",
                (channel, account, chat, replay),
            ).fetchone()
            if row is None:
                return None
            if row[2] != switch_arg:
                raise OutboundIdentityConflictError(
                    "switch replay identity identifies another argument"
                )
            return {"agent_id": row[0], "intent_ids": list(row[1])}

    def native_selections(self, channel: str, account: str) -> dict[str, int]:
        """Derived subscriptions restore only canonical selections belonging to this account."""
        with self._pool().connection() as conn:
            rows = conn.execute(
                "SELECT chat_id,push_agent_id FROM im_bridge_cursors WHERE channel=%s AND push_account_id=%s AND push_agent_id IS NOT NULL",
                (channel, account),
            ).fetchall()
            return dict(rows)

    def native_selection(self, channel: str, account: str, chat: str) -> int | None:
        """Read only a matching native account selection, never import unbound JSON."""
        with self._pool().connection() as conn:
            row = conn.execute(
                "SELECT push_agent_id FROM im_bridge_cursors WHERE channel=%s AND chat_id=%s AND push_account_id=%s",
                (channel, chat, account),
            ).fetchone()
            return row[0] if row is not None else None

    def selection(
        self, channel: str, account: str, chat: str, legacy_agent: int | None
    ) -> int | None:
        """Import the existing JSON selection once into an uninitialized cursor.

        Importing the selection does not qualify legacy history or advance its
        position. Initialized rows, including explicit clears, always win.
        """
        with write_transaction(self._pool()) as conn:
            conn.execute(
                "INSERT INTO im_bridge_cursors(channel,chat_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (channel, chat),
            )
            saved_account, selected, _, initialized = self._lock_cursor(conn, channel, chat)
            if saved_account is not None and saved_account != account:
                raise OutboundAccountMismatchError("push cursor belongs to another adapter account")
            if not initialized and saved_account is None:
                conn.execute(
                    "UPDATE im_bridge_cursors SET push_agent_id=%s, push_account_id=%s, "
                    "push_item_id=CASE WHEN push_agent_id IS NOT DISTINCT FROM %s THEN push_item_id END, "
                    "push_created_at=CASE WHEN push_agent_id IS NOT DISTINCT FROM %s THEN push_created_at END "
                    "WHERE channel=%s AND chat_id=%s",
                    (legacy_agent, account, legacy_agent, legacy_agent, channel, chat),
                )
                selected = legacy_agent
            return selected

    def selections(self) -> dict[tuple[str, str], int | None]:
        with self._pool().connection() as conn:
            return {
                (channel, chat): agent
                for channel, chat, agent in conn.execute(
                    "SELECT channel,chat_id,push_agent_id FROM im_bridge_cursors WHERE push_initialized OR push_account_id IS NOT NULL"
                ).fetchall()
            }

    def restore_candidates(
        self, legacy: dict[tuple[str, str], int]
    ) -> dict[tuple[str, str], int | None]:
        """Bound cursors win; unbound legacy rows use the existing JSON choice, including clear."""
        result: dict[tuple[str, str], int | None] = dict(legacy)
        with self._pool().connection() as conn:
            rows = conn.execute(
                "SELECT channel,chat_id,push_agent_id,push_account_id,push_initialized FROM im_bridge_cursors"
            ).fetchall()
        for channel, chat, agent, account, initialized in rows:
            if initialized or account is not None:
                result[channel, chat] = agent
            else:
                result.setdefault((channel, chat), None)
        return result

    def clear_selection(
        self,
        channel: str,
        account: str,
        chat: str,
        expected: int,
        *,
        guard: Callable[[Connection], None] | None = None,
    ) -> bool:
        with write_transaction(self._pool()) as conn:
            if guard is not None:
                guard(conn)
            return (
                conn.execute(
                    "UPDATE im_bridge_cursors SET push_agent_id=NULL, push_item_id=NULL, push_created_at=NULL, "
                    "push_initialized=TRUE WHERE channel=%s AND chat_id=%s AND push_account_id=%s AND push_agent_id=%s",
                    (channel, chat, account, expected),
                ).rowcount
                == 1
            )

    def mark_unavailable_accounts(self, accounts: dict[str, str]) -> None:
        """Hold old queued accounts with a credential-free diagnostic; never retarget."""
        with write_transaction(self._pool()) as conn:
            conn.execute(
                "UPDATE im_bridge_outbound_intents SET outcome_reason='authenticated_account_unavailable' "
                "WHERE status='queued' AND (channel,account_id) NOT IN "
                "(SELECT key,value FROM jsonb_each_text(%s)) "
                "AND outcome_reason IS DISTINCT FROM 'authenticated_account_unavailable'",
                (Jsonb(accounts),),
            )

    def pending_streams(self, accounts: dict[str, str]) -> list[tuple[str, str, str]]:
        if not accounts:
            return []
        with self._pool().connection() as conn:
            return list(
                conn.execute(
                    "SELECT channel,account_id,chat_id FROM im_bridge_outbound_intents "
                    "WHERE status IN ('queued','sending') AND (channel,account_id) IN "
                    "(SELECT key,value FROM jsonb_each_text(%s)) GROUP BY channel,account_id,chat_id "
                    "ORDER BY min(id) LIMIT 16",
                    (Jsonb(accounts),),
                ).fetchall()
            )

    def claim(self, stream: tuple[str, str, str]) -> tuple[int, UUID, OutboundIntent] | None:
        """Recover and claim only while the caller holds this stream's transaction gate.

        The gate must outlive the external call and finish commit. There is no
        time-based claim stealing: a live owner can retain its gate indefinitely.
        """
        with write_transaction(self._pool()) as conn:
            conn.execute(
                "UPDATE im_bridge_outbound_intents SET status='uncertain', completed_at=now(), "
                "outcome_reason='sending_attempt_unresolved' "
                "WHERE channel=%s AND account_id=%s AND chat_id=%s AND status='sending'",
                stream,
            )
            row = conn.execute(
                "SELECT id,request FROM im_bridge_outbound_intents "
                "WHERE channel=%s AND account_id=%s AND chat_id=%s AND status='queued' "
                "ORDER BY id LIMIT 1 FOR UPDATE",
                stream,
            ).fetchone()
            if row is None:
                return None
            attempt = uuid4()
            conn.execute(
                "UPDATE im_bridge_outbound_intents SET status='sending', attempt_id=%s, started_at=now(), "
                "outcome_reason=NULL WHERE id=%s AND status='queued'",
                (attempt, row[0]),
            )
            return int(row[0]), attempt, OutboundIntent.model_validate(row[1])

    def finish(
        self, intent_id: int, attempt_id: UUID, status: OutboundStatus, reason: str | None
    ) -> bool:
        if status not in (OutboundStatus.SENT, OutboundStatus.UNCERTAIN, OutboundStatus.FAILED):
            raise ValueError("a sending attempt requires a terminal outcome")
        with write_transaction(self._pool()) as conn:
            row = conn.execute(
                "UPDATE im_bridge_outbound_intents SET status=%s, outcome_reason=%s, completed_at=now() "
                "WHERE id=%s AND attempt_id=%s AND status='sending' RETURNING source_kind,source_id",
                (status.value, reason, intent_id, attempt_id),
            ).fetchone()
            if row is not None and status == OutboundStatus.SENT and row[0] == "alert_group":
                stamp_native_sent(conn, int(row[1]))
            return row is not None
