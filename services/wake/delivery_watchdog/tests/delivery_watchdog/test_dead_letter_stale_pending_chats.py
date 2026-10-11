"""Dead-letter stale pending chats without changing live-owner retry windows."""

from __future__ import annotations

import psycopg
from psycopg_pool import ConnectionPool

from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from services.wake.delivery_watchdog.tests.test_delivery_watchdog import (
    healthy_host_verdict as healthy_host_verdict,
)
from services.wake.delivery_watchdog.tests.test_delivery_watchdog import (
    idling_agent,
    pending_resurrect_row,
    terminated_agent,
)
from services.wake.delivery_watchdog.tests.test_delivery_watchdog import pool as pool


class TestDeadLetterStalePendingChats:
    def test_old_pending_chat_of_terminated_owner_is_dead_lettered(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """Issue #2049: a chat that never claimed its terminated owner is
        archived once past the threshold instead of resurrecting it forever."""
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_pending_chats

        aid = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        row = pending_resurrect_row(db_conn, aid, age_s=2 * 86400, kind="chat")

        assert dead_letter_stale_pending_chats(pool, 86400.0) == 1
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status, claimed_at IS NOT NULL FROM inbound_messages WHERE id = %s",
                (row,),
            )
            assert cur.fetchone() == ("done", True)

    def test_fresh_pending_chat_of_terminated_owner_is_untouched(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """A recent pending chat is still a live resurrect candidate — the G4
        retry window must stay open until the threshold closes it."""
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_pending_chats

        aid = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        row = pending_resurrect_row(db_conn, aid, age_s=60, kind="chat")

        assert dead_letter_stale_pending_chats(pool, 86400.0) == 0
        with db_conn.cursor() as cur:
            cur.execute("SELECT status, claimed_at FROM inbound_messages WHERE id = %s", (row,))
            assert cur.fetchone() == ("pending", None)

    def test_old_pending_chat_of_live_owner_is_untouched(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """Live owners keep their pending chats: only terminated owners have no
        consumer, so the sweep never touches idling/running queues."""
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_pending_chats

        aid = idling_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        row = pending_resurrect_row(db_conn, aid, age_s=2 * 86400, kind="chat")

        assert dead_letter_stale_pending_chats(pool, 86400.0) == 0
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (row,))
            assert cur.fetchone() == ("pending",)

    def test_old_non_chat_pending_row_is_untouched(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """Lifecycle kinds keep their own sweep; this one is chat-only."""
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_pending_chats

        aid = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        row = pending_resurrect_row(db_conn, aid, age_s=2 * 86400, kind="terminate")

        assert dead_letter_stale_pending_chats(pool, 86400.0) == 0
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (row,))
            assert cur.fetchone() == ("pending",)
