"""Resurrection triggers respect termination and force fences."""

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

import base.db
from base.agents.messages.inbound import InboundKind
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops.agents import resurrect_agent
from ops.agents.wake import ResurrectTriggerStaleError
from ops.lifecycle import force_mark_terminated
from ops.lifecycle.tests.resurrection_setup import (
    agents_row,
    hosted_agent,
    inbound_rows,
    noop,
    settle_hosted_force,
)
from ops.tests.pool_support import make_test_pool


class TestResurrectAgent:
    async def test_repeat_force_fence_preserves_page_reopen_epoch(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        aops_pool: AsyncConnectionPool,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """A repeated force creates a newer intent fence without changing the
        real status-transition epoch used to reopen pages on manual resurrect."""
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        monkeypatch.setattr("ops.lifecycle.termination.publish_inbound_wake", noop)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_pages (agent_id, name, port) VALUES (%s, 'work', 8765)",
                (agent_id,),
            )
        db_conn.commit()

        with make_test_pool() as pool:
            force_mark_terminated(database, event_bus, agent_id, pool)
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT status_changed_at, last_force_terminate_inbound_id "
                    "FROM agents_meta WHERE id = %s",
                    (agent_id,),
                )
                first_agent_row = cur.fetchone()
                cur.execute(
                    "SELECT closed_at FROM agent_pages WHERE agent_id = %s AND name = 'work'",
                    (agent_id,),
                )
                first_page_row = cur.fetchone()
            assert first_agent_row is not None and first_page_row is not None
            assert first_page_row[0] == first_agent_row[0]

            force_mark_terminated(database, event_bus, agent_id, pool)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status_changed_at, last_force_terminate_inbound_id "
                "FROM agents_meta WHERE id = %s",
                (agent_id,),
            )
            repeated_agent_row = cur.fetchone()
            cur.execute(
                "SELECT closed_at FROM agent_pages WHERE agent_id = %s AND name = 'work'",
                (agent_id,),
            )
            repeated_page_row = cur.fetchone()
        assert repeated_agent_row is not None and repeated_page_row is not None
        assert repeated_agent_row[0] == first_agent_row[0]
        assert repeated_agent_row[1] > first_agent_row[1]
        assert repeated_page_row[0] == first_page_row[0]

        await settle_hosted_force(db_conn, aops_pool, agent_id)

        resurrect_agent(database, event_bus, agent_id, resurrected_by="user")

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT closed_at FROM agent_pages WHERE agent_id = %s AND name = 'work'",
                (agent_id,),
            )
            reopened_page_row = cur.fetchone()
        assert reopened_page_row == (None,)

    async def test_guarded_resurrect_rejects_chat_below_latest_force_fence(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        aops_pool: AsyncConnectionPool,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """Even without a real status transition, a repeated explicit force
        fences every chat inbound that existed before that latest intent."""
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        monkeypatch.setattr("ops.lifecycle.termination.publish_inbound_wake", noop)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        trigger_id = base.db.insert_inbound_message(
            db_conn,
            agent_id,
            "work before repeated force",
            source="user",
            bus=event_bus,
            database=database,
        )
        with make_test_pool() as pool:
            force_mark_terminated(database, event_bus, agent_id, pool)

        await settle_hosted_force(db_conn, aops_pool, agent_id)

        with pytest.raises(ResurrectTriggerStaleError, match="trigger work no longer qualifies"):
            resurrect_agent(
                database,
                event_bus,
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=trigger_id,
                trigger_inbound_kind=InboundKind.CHAT,
            )

        row = agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "terminated"

    def test_guarded_resurrect_rejects_chat_from_prior_termination(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """A watchdog task selected for one death must not revive a later
        explicit kill while its RPC was in flight."""
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        trigger_id = base.db.insert_inbound_message(
            db_conn,
            agent_id,
            "wake after first death",
            source="user",
            bus=event_bus,
            database=database,
        )

        # The agent came back by another path and was explicitly killed again
        # before the watchdog's original RPC reached its home runner.
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'idling' WHERE id = %s", (agent_id,))
        db_conn.commit()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
            cur.execute(
                "UPDATE inbound_messages "
                "SET created_at = (SELECT status_changed_at FROM agents_meta WHERE id = %s) "
                "                 - interval '1 second' "
                "WHERE id = %s",
                (agent_id, trigger_id),
            )
        db_conn.commit()

        with pytest.raises(ResurrectTriggerStaleError, match="trigger work no longer qualifies"):
            resurrect_agent(
                database,
                event_bus,
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=trigger_id,
                trigger_inbound_kind=InboundKind.CHAT,
            )

        row = agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "terminated"
        assert inbound_rows(db_conn, agent_id) == [("wake after first death", "chat", "user")]

    def test_guarded_resurrect_rejects_chat_that_is_no_longer_pending(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """A trigger claimed while the RPC is in flight no longer justifies
        launching the terminated owner."""
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        trigger_id = base.db.insert_inbound_message(
            db_conn, agent_id, "already handled", source="user", bus=event_bus, database=database
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages SET status = 'claimed', claimed_at = now() WHERE id = %s",
                (trigger_id,),
            )
        db_conn.commit()

        with pytest.raises(ResurrectTriggerStaleError, match="trigger work no longer qualifies"):
            resurrect_agent(
                database,
                event_bus,
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=trigger_id,
                trigger_inbound_kind=InboundKind.CHAT,
            )

        row = agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "terminated"
        assert inbound_rows(db_conn, agent_id) == [("already handled", "chat", "user")]

    def test_guarded_resurrect_accepts_pending_chat_after_current_termination(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """A pending chat created after the current death still auto-wakes the
        agent, preserving the post-termination delivery contract."""
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        trigger_id = base.db.insert_inbound_message(
            db_conn,
            agent_id,
            "new work after death",
            source="user",
            bus=event_bus,
            database=database,
        )
        returned = resurrect_agent(
            database,
            event_bus,
            agent_id,
            resurrected_by="system",
            trigger_inbound_id=trigger_id,
            trigger_inbound_kind=InboundKind.CHAT,
        )

        assert returned == agent_id
        row = agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "idling"
        assert inbound_rows(db_conn, agent_id) == [
            ("new work after death", "chat", "user"),
            ("", "resurrect", "system"),
        ]

    def test_guarded_resurrect_accepts_exact_pending_compact_after_termination(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """UI compact is guarded work too: its exact durable id and expected
        kind qualify only while pending after the current death."""
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        compact_id = base.db.insert_inbound_message(
            db_conn,
            agent_id,
            "",
            source="user",
            kind="compact_request",
            bus=event_bus,
            database=database,
        )
        returned = resurrect_agent(
            database,
            event_bus,
            agent_id,
            resurrected_by="system",
            trigger_inbound_id=compact_id,
            trigger_inbound_kind=InboundKind.COMPACT_REQUEST,
        )

        assert returned == agent_id
        assert agents_row(db_conn, agent_id)[2] == "idling"  # type: ignore[index]
        assert inbound_rows(db_conn, agent_id) == [
            ("", "compact_request", "user"),
            ("", "resurrect", "system"),
        ]

    def test_guarded_compact_rejects_kind_mismatch_and_claimed_trigger(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """The caller's expected kind is part of the CAS, and a compact that
        has already been claimed no longer licenses a new process."""
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        compact_id = base.db.insert_inbound_message(
            db_conn,
            agent_id,
            "",
            source="user",
            kind="compact_request",
            bus=event_bus,
            database=database,
        )

        with pytest.raises(ResurrectTriggerStaleError):
            resurrect_agent(
                database,
                event_bus,
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=compact_id,
                trigger_inbound_kind=InboundKind.CHAT,
            )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages SET status = 'claimed', claimed_at = now() WHERE id = %s",
                (compact_id,),
            )
        db_conn.commit()
        with pytest.raises(ResurrectTriggerStaleError):
            resurrect_agent(
                database,
                event_bus,
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=compact_id,
                trigger_inbound_kind=InboundKind.COMPACT_REQUEST,
            )

        assert agents_row(db_conn, agent_id)[2] == "terminated"  # type: ignore[index]

    async def test_guarded_compact_below_force_fence_is_stale(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        aops_pool: AsyncConnectionPool,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """A force after compact enqueue fences that older work exactly like
        chat, even though no second status transition occurs."""
        agent_id = hosted_agent(
            db_conn,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        monkeypatch.setattr("ops.lifecycle.termination.publish_inbound_wake", noop)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        compact_id = base.db.insert_inbound_message(
            db_conn,
            agent_id,
            "",
            source="user",
            kind="compact_request",
            bus=event_bus,
            database=database,
        )
        with make_test_pool() as pool:
            force_mark_terminated(database, event_bus, agent_id, pool)

        await settle_hosted_force(db_conn, aops_pool, agent_id)

        with pytest.raises(ResurrectTriggerStaleError):
            resurrect_agent(
                database,
                event_bus,
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=compact_id,
                trigger_inbound_kind=InboundKind.COMPACT_REQUEST,
            )
        assert agents_row(db_conn, agent_id)[2] == "terminated"  # type: ignore[index]
