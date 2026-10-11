"""Delivery watchdog cases: select terminated owners with pending."""

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config.service_read import ConfigAuthority
from base.db import Database, insert_inbound_message
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.wake.delivery_watchdog.tests.test_delivery_watchdog import (
    _backdate_chat_before_termination,
    _make_reaped_crash_agent,
    idling_agent,
    terminated_agent,
)
from services.wake.delivery_watchdog.tests.test_delivery_watchdog import (
    healthy_host_verdict as healthy_host_verdict,
)
from services.wake.delivery_watchdog.tests.test_delivery_watchdog import (
    pool as pool,
)


class TestSelectTerminatedOwnersWithPending:
    def test_force_fence_excludes_older_chat_but_accepts_newer_chat(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """The selector uses the monotonic explicit-kill fence in addition to
        wall-clock status time: old queued work stays dead, later work wakes."""
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        old_chat_id = insert_inbound_message(
            db_conn, aid, "before force", source="user", bus=event_bus, database=database
        )
        fence_id = insert_inbound_message(
            db_conn,
            aid,
            "",
            source="user",
            kind="terminate",
            bus=event_bus,
            database=database,
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET last_force_terminate_inbound_id = %s WHERE id = %s",
                (fence_id, aid),
            )
        db_conn.commit()

        assert old_chat_id < fence_id
        assert select_terminated_owners_with_pending(pool, 86400.0) == []

        new_chat_id = insert_inbound_message(
            db_conn, aid, "after force", source="user", bus=event_bus, database=database
        )
        assert new_chat_id > fence_id
        assert select_terminated_owners_with_pending(pool, 86400.0) == [(aid, new_chat_id)]

    def test_ignores_pending_chat_that_predates_latest_termination(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """A user's explicit kill wins over mail already waiting when they
        killed the agent; that old row must not immediately undo the kill."""
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = idling_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        iid = insert_inbound_message(
            db_conn, aid, "already waiting", source="user", bus=event_bus, database=database
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated', termination_source = 'user' "
                "WHERE id = %s",
                (aid,),
            )
            cur.execute(
                "UPDATE inbound_messages "
                "SET created_at = (SELECT status_changed_at FROM agents_meta WHERE id = %s) "
                "                 - interval '1 second' "
                "WHERE id = %s",
                (aid, iid),
            )
        db_conn.commit()

        assert select_terminated_owners_with_pending(pool, 86400.0) == []

    def test_returns_pending_chat_created_after_latest_termination(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """A new chat sent after termination preserves the existing contract:
        delivery to a dead agent wakes it automatically."""
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        iid = insert_inbound_message(
            db_conn, aid, "new request", source="user", bus=event_bus, database=database
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages "
                "SET created_at = (SELECT status_changed_at FROM agents_meta WHERE id = %s) "
                "                 + interval '1 second' "
                "WHERE id = %s",
                (aid, iid),
            )
        db_conn.commit()

        assert select_terminated_owners_with_pending(pool, 86400.0) == [(aid, iid)]

    def test_returns_terminated_owners_with_pending_chat(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        iid = insert_inbound_message(
            db_conn, aid, "hello?", source="user", bus=event_bus, database=database
        )

        assert select_terminated_owners_with_pending(pool, 86400.0) == [(aid, iid)]

    def test_deduplicates_per_agent(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """250 dead letters for one agent mean ONE resurrect, not 250."""
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        iids: list[int] = []
        for _ in range(3):
            iids.append(
                insert_inbound_message(
                    db_conn, aid, "hello?", source="user", bus=event_bus, database=database
                )
            )

        assert select_terminated_owners_with_pending(pool, 86400.0) == [(aid, min(iids))]

    def test_ignores_live_owners_and_non_chat_kinds(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        live = idling_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )  # idling owner — not a resurrect case
        insert_inbound_message(db_conn, live, "hi", source="user", bus=event_bus, database=database)
        dead = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (%s, %s, 'restart', 'system')",
                (dead, ""),
            )
        db_conn.commit()

        assert select_terminated_owners_with_pending(pool, 86400.0) == []

    def test_claimed_chat_is_not_retried(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        iid = insert_inbound_message(
            db_conn, aid, "hello?", source="user", bus=event_bus, database=database
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status = 'claimed' WHERE id = %s", (iid,))
        db_conn.commit()

        assert select_terminated_owners_with_pending(pool, 86400.0) == []

    def test_wake_suppression_excludes_until_expiry(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        iid = insert_inbound_message(
            db_conn,
            aid,
            "queued during suppression",
            source="agent:1",
            bus=event_bus,
            database=database,
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET wake_suppressed_until = now() + interval '1 hour', "
                "wake_suppress_reason = 'resurrect_failed' WHERE id = %s",
                (aid,),
            )
        db_conn.commit()

        assert select_terminated_owners_with_pending(pool, 86400.0) == []

        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET wake_suppressed_until = now() - interval '1 second' "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert select_terminated_owners_with_pending(pool, 86400.0) == [(aid, iid)]

    def test_stale_pending_chat_does_not_resurrect_owner(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """Issue #2049: the ghost-alive state — a terminated owner whose only
        pending chats are past the stale threshold — is not a resurrect trigger."""
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        iid = insert_inbound_message(
            db_conn, aid, "stale peer mail", source="agent:1", bus=event_bus, database=database
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages SET created_at = now() - interval '2 days' WHERE id = %s",
                (iid,),
            )
            cur.execute(
                "UPDATE agents_meta SET status_changed_at = now() - interval '3 days' "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()

        assert select_terminated_owners_with_pending(pool, 86400.0) == []

    def test_recent_pending_chat_still_resurrects_owner(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """Inside the threshold the G4 retry window is unchanged: a recent
        post-termination chat still wakes its terminated owner."""
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        iid = insert_inbound_message(
            db_conn, aid, "fresh peer mail", source="agent:1", bus=event_bus, database=database
        )

        assert select_terminated_owners_with_pending(pool, 86400.0) == [(aid, iid)]

    # ── Task #3617: system-reaped crash rows resume their leftover work ──────

    def test_system_reaped_crash_row_resumes_leftover_chat(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """A chat already waiting when the SYSTEM reaped a crash-marked corpse
        is leftover work, not mail an operator's kill cancelled — the relaxed
        fence lets it trigger resurrection (task #3617, design #3610 section 6)."""
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_reaped_crash_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        iid = insert_inbound_message(
            db_conn, aid, "leftover work", source="user", bus=event_bus, database=database
        )
        _backdate_chat_before_termination(db_conn, aid, iid)

        assert select_terminated_owners_with_pending(pool, 86400.0) == [(aid, iid)]

    def test_relaxed_guard_still_requires_the_crash_marker(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """`reaper` alone — marker already cleared by a completed turn of the
        revived incarnation — is an ordinary system death: the fence holds."""
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET termination_source = 'reaper', "
                "last_turn_fatal_at = NULL WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        iid = insert_inbound_message(
            db_conn, aid, "leftover work", source="user", bus=event_bus, database=database
        )
        _backdate_chat_before_termination(db_conn, aid, iid)

        assert select_terminated_owners_with_pending(pool, 86400.0) == []

    @pytest.mark.parametrize("source", ["user", "exit", "launch-confirm", "integrity"])
    def test_relaxed_guard_requires_reaper_source(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        source: str,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """The crash marker alone never relaxes the fence: only the SYSTEM's
        own reap is not an operator decision (user/exit) and not a launch or
        integrity death (which keep their own semantics)."""
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET termination_source = %s, "
                "last_turn_fatal_at = now() - interval '1 hour' WHERE id = %s",
                (source, aid),
            )
        db_conn.commit()
        iid = insert_inbound_message(
            db_conn, aid, "leftover work", source="user", bus=event_bus, database=database
        )
        _backdate_chat_before_termination(db_conn, aid, iid)

        assert select_terminated_owners_with_pending(pool, 86400.0) == []

    def test_relaxed_guard_keeps_suppression_and_breaker_gates(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """The relaxed fence does not bypass the automatic-recovery gates: an
        active wake suppression refuses, an expired one does not, and a
        tripped recovery circuit breaker refuses even without a window
        (task #3617; the streak is the durable gate)."""
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_reaped_crash_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        iid = insert_inbound_message(
            db_conn, aid, "leftover work", source="user", bus=event_bus, database=database
        )
        _backdate_chat_before_termination(db_conn, aid, iid)

        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET wake_suppressed_until = now() + interval '1 hour', "
                "wake_suppress_reason = 'resurrect_failed' WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert select_terminated_owners_with_pending(pool, 86400.0) == []

        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET wake_suppressed_until = now() - interval '1 second' "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert select_terminated_owners_with_pending(pool, 86400.0) == [(aid, iid)]

        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET permanent_reject_streak = 1 WHERE id = %s", (aid,))
        db_conn.commit()
        # One rejection is not a halt: the first fresh-resolve window stays open.
        assert select_terminated_owners_with_pending(pool, 86400.0) == [(aid, iid)]

        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET permanent_reject_streak = 2 WHERE id = %s", (aid,))
        db_conn.commit()
        assert select_terminated_owners_with_pending(pool, 86400.0) == []

    def test_relaxed_guard_still_bounds_age_and_keeps_the_force_fence(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """The remaining conjuncts are untouched: past the dead-letter bound
        the row is no trigger, and a later explicit force fence still wins."""
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_reaped_crash_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        iid = insert_inbound_message(
            db_conn, aid, "leftover work", source="user", bus=event_bus, database=database
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages SET created_at = now() - interval '2 hours' WHERE id = %s",
                (iid,),
            )
        db_conn.commit()

        assert select_terminated_owners_with_pending(pool, 3600.0) == []
        assert select_terminated_owners_with_pending(pool, 86400.0) == [(aid, iid)]

        fence = insert_inbound_message(
            db_conn, aid, "", source="user", kind="terminate", bus=event_bus, database=database
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET last_force_terminate_inbound_id = %s WHERE id = %s",
                (fence, aid),
            )
        db_conn.commit()
        assert select_terminated_owners_with_pending(pool, 86400.0) == []

    def test_relaxed_guard_still_refuses_failed_restart(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """A failed-restart target keeps its own hard fence: the relaunch
        observation must settle before any resurrection, reaped crash row or
        not."""
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_reaped_crash_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        iid = insert_inbound_message(
            db_conn, aid, "leftover work", source="user", bus=event_bus, database=database
        )
        _backdate_chat_before_termination(db_conn, aid, iid)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET runtime_generation = gen_random_uuid(), "
                "runtime_owner = gen_random_uuid() WHERE id = %s",
                (aid,),
            )
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, status, "
                "target_generation, target_owner, claimed_at, applied_at, payload) "
                "SELECT id, '', 'restart', 'system', 'done', runtime_generation, "
                "runtime_owner, now(), now(), "
                '\'{"lifecycle_result": {"outcome": "failed", '
                '"reason": "restart_deadline_expired"}}\'::jsonb '
                "FROM agents_meta WHERE id = %s",
                (aid,),
            )
        db_conn.commit()

        assert select_terminated_owners_with_pending(pool, 86400.0) == []

    def test_system_notice_chat_never_selected(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """A system-family chat is a platform notification, never a resurrect
        trigger: plain 'system' and every 'system:<subtype>' variant must not
        select, while a real chat on the same owner still does (task #3687 —
        the watcher-reap notice that woke 6260 twice)."""
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        insert_inbound_message(
            db_conn, aid, "notice", source="system", bus=event_bus, database=database
        )
        insert_inbound_message(
            db_conn, aid, "variant", source="system:notice-reply", bus=event_bus, database=database
        )

        assert select_terminated_owners_with_pending(pool, 86400.0) == []

        real = insert_inbound_message(
            db_conn, aid, "real chat", source="user", bus=event_bus, database=database
        )
        assert select_terminated_owners_with_pending(pool, 86400.0) == [(aid, real)]

    def test_machine_wakeup_chats_still_select(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """Machine wakeups (watcher: / shell: / schedule:) are deliberately NOT
        notices: a crash-reaped owner's watcher wake is a revival channel, so
        they must keep selecting (task #3687 boundary review)."""
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        wid = insert_inbound_message(
            db_conn, aid, "wake", source="watcher:3", bus=event_bus, database=database
        )

        assert select_terminated_owners_with_pending(pool, 86400.0) == [(aid, wid)]

    def test_hosted_turn_recovery_marked_chat_still_selects(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        """The watchdog's hosted-turn recovery chat is the one system-source
        chat that must stay selected: it is this scan's durable retry for a
        wedged hosted turn, so the payload marker flips the verdict on the G4
        channel too. Only the exact JSON boolean `true` counts — a string
        "true" fails closed (task #3687 review, Ava #3242)."""
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = terminated_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        insert_inbound_message(
            db_conn,
            aid,
            "string marker",
            source="system",
            payload={"hosted_turn_recovery": "true"},
            bus=event_bus,
            database=database,
        )
        assert select_terminated_owners_with_pending(pool, 86400.0) == []

        recovery = insert_inbound_message(
            db_conn,
            aid,
            "continue from the latest checkpoint",
            source="system",
            payload={"hosted_turn_recovery": True},
            bus=event_bus,
            database=database,
        )
        assert select_terminated_owners_with_pending(pool, 86400.0) == [(aid, recovery)]
