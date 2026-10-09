"""Delivery watchdog cases: dispatch backoff and poison."""

from __future__ import annotations

import time

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base import telemetry
from base.config.service_read import ConfigAuthority
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.wake.delivery_watchdog.daemon import (
    dispatch_wakes,
    select_pending_for_dispatch,
    select_pending_ids,
)
from services.wake.delivery_watchdog.tests.test_delivery_watchdog import (
    _DISPATCH_BACKOFF_STEPS_S,
    _DISPATCH_THRESHOLD_S,
    _HOST_STALENESS_S,
    _MAX_DISPATCH_COUNT,
    _delivery_poisoned_events,
    _insert_claimed_row,
    _insert_old_inbound,
    _insert_pending_resurrect_row,
    _make_idling_agent,
    _make_running_agent,
    _make_terminated_agent,
    _wait_for_poisoned_events,
)
from services.wake.delivery_watchdog.tests.test_delivery_watchdog import (
    _healthy_host_verdict as _healthy_host_verdict,
)
from services.wake.delivery_watchdog.tests.test_delivery_watchdog import (
    pool as pool,
)


class TestDispatchBackoffAndPoison:
    @staticmethod
    def _dispatch(pool: ConnectionPool) -> int:
        return dispatch_wakes(
            pool,
            Database.from_settings(),
            EventBus.from_settings(),
            _DISPATCH_THRESHOLD_S,
            _MAX_DISPATCH_COUNT,
            _DISPATCH_BACKOFF_STEPS_S,
            _HOST_STALENESS_S,
        )

    @staticmethod
    def _set_last_dispatch_age(db: psycopg.Connection, inbound_id: int, age_s: float) -> None:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages "
                "SET last_dispatch_at = clock_timestamp() - make_interval(secs => %s) "
                "WHERE id = %s",
                (age_s, inbound_id),
            )
        db.commit()

    def test_first_dispatch_records_count_and_timestamp(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        calls: list[tuple[int, str]] = []

        def record_publish(_db: object, _bus: object, agent_id: int, payload: str) -> bool:
            calls.append((agent_id, payload))
            return True

        monkeypatch.setattr("base.db.publish_inbound_wake", record_publish)

        assert self._dispatch(pool) == 1
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT dispatch_count, last_dispatch_at IS NOT NULL "
                "FROM inbound_messages WHERE id = %s",
                (iid,),
            )
            row = cur.fetchone()
        assert row == (1, True)
        assert calls == [(aid, str(iid))]

    def test_backoff_blocks_until_current_step_elapses(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)

        def accept_publish(_db: object, _bus: object, _agent_id: int, _payload: str) -> bool:
            return True

        monkeypatch.setattr("base.db.publish_inbound_wake", accept_publish)
        assert self._dispatch(pool) == 1

        assert (
            select_pending_for_dispatch(
                pool,
                _DISPATCH_THRESHOLD_S,
                _MAX_DISPATCH_COUNT,
                _DISPATCH_BACKOFF_STEPS_S,
                _HOST_STALENESS_S,
            )
            == []
        )
        self._set_last_dispatch_age(db_conn, iid, 5.5)
        assert select_pending_for_dispatch(
            pool,
            _DISPATCH_THRESHOLD_S,
            _MAX_DISPATCH_COUNT,
            _DISPATCH_BACKOFF_STEPS_S,
            _HOST_STALENESS_S,
        ) == [(iid, aid)]
        self._set_last_dispatch_age(db_conn, iid, 4.5)
        assert (
            select_pending_for_dispatch(
                pool,
                _DISPATCH_THRESHOLD_S,
                _MAX_DISPATCH_COUNT,
                _DISPATCH_BACKOFF_STEPS_S,
                _HOST_STALENESS_S,
            )
            == []
        )
        self._set_last_dispatch_age(db_conn, iid, 1.5)
        assert select_pending_for_dispatch(
            pool,
            _DISPATCH_THRESHOLD_S,
            _MAX_DISPATCH_COUNT,
            [1.0],
            _HOST_STALENESS_S,
        ) == [(iid, aid)]

    def test_publish_failure_does_not_increment_count(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)

        def fail_publish(_db: object, _bus: object, *_args: object) -> bool:
            return False

        monkeypatch.setattr("base.db.publish_inbound_wake", fail_publish)
        assert self._dispatch(pool) == 0
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT dispatch_count, last_dispatch_at FROM inbound_messages WHERE id = %s",
                (iid,),
            )
            row = cur.fetchone()
        assert row == (0, None)

    def test_claimed_mid_dispatch_is_not_counted(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)

        def publish_and_claim(_db: object, _bus: object, agent_id: int, payload: str) -> bool:
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE inbound_messages SET status = 'claimed' WHERE id = %s",
                    (iid,),
                )
            db_conn.commit()
            return True

        monkeypatch.setattr("base.db.publish_inbound_wake", publish_and_claim)
        assert self._dispatch(pool) == 1
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status, dispatch_count, poisoned_at FROM inbound_messages WHERE id = %s",
                (iid,),
            )
            row = cur.fetchone()
        assert row == ("claimed", 0, None)

    def test_dispatch_cap_poisons_once_and_emits_event(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)

        def accept_publish(_db: object, _bus: object, _agent_id: int, _payload: str) -> bool:
            return True

        monkeypatch.setattr("base.db.publish_inbound_wake", accept_publish)

        for _ in range(_MAX_DISPATCH_COUNT):
            self._set_last_dispatch_age(db_conn, iid, 1000.0)
            assert self._dispatch(pool) == 1

        assert (
            select_pending_for_dispatch(
                pool,
                _DISPATCH_THRESHOLD_S,
                _MAX_DISPATCH_COUNT,
                _DISPATCH_BACKOFF_STEPS_S,
                _HOST_STALENESS_S,
            )
            == []
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT dispatch_count, poisoned_at IS NOT NULL, status "
                "FROM inbound_messages WHERE id = %s",
                (iid,),
            )
            row = cur.fetchone()
        assert row == (_MAX_DISPATCH_COUNT, True, "pending")

        events = _wait_for_poisoned_events(aid)
        assert len(events) == 1
        assert events[0]["level"] == "warning"
        attributes = events[0]["attributes"]
        assert isinstance(attributes, dict)
        assert attributes["inbound_id"] == iid
        assert attributes["dispatch_count"] == _MAX_DISPATCH_COUNT

        assert self._dispatch(pool) == 0
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            telemetry.flush()
            time.sleep(0.05)
        assert len(_delivery_poisoned_events(aid)) == 1

    def test_poisoned_row_is_not_dispatched_after_backoff(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages SET poisoned_at = clock_timestamp(), "
                "last_dispatch_at = clock_timestamp() - interval '1000 seconds' "
                "WHERE id = %s",
                (iid,),
            )
        db_conn.commit()
        calls: list[tuple[int, str]] = []

        def record_unexpected_publish(
            _db: object, _bus: object, agent_id: int, payload: str
        ) -> bool:
            calls.append((agent_id, payload))
            return True

        monkeypatch.setattr("base.db.publish_inbound_wake", record_unexpected_publish)

        assert self._dispatch(pool) == 0
        assert calls == []

    def test_manual_reset_restores_dispatch(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages SET dispatch_count = %s, "
                "last_dispatch_at = clock_timestamp(), poisoned_at = clock_timestamp() "
                "WHERE id = %s",
                (_MAX_DISPATCH_COUNT, iid),
            )
            cur.execute(
                "UPDATE inbound_messages SET dispatch_count = 0, "
                "last_dispatch_at = NULL, poisoned_at = NULL WHERE id = %s",
                (iid,),
            )
        db_conn.commit()
        calls: list[tuple[int, str]] = []

        def record_publish(_db: object, _bus: object, agent_id: int, payload: str) -> bool:
            calls.append((agent_id, payload))
            return True

        monkeypatch.setattr("base.db.publish_inbound_wake", record_publish)

        assert self._dispatch(pool) == 1
        assert calls == [(aid, str(iid))]
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT dispatch_count, poisoned_at FROM inbound_messages WHERE id = %s",
                (iid,),
            )
            row = cur.fetchone()
        assert row == (1, None)

    def test_dispatch_storm_is_bounded_by_cap(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        calls: list[tuple[int, str]] = []

        def record_publish(_db: object, _bus: object, agent_id: int, payload: str) -> bool:
            calls.append((agent_id, payload))
            return True

        monkeypatch.setattr("base.db.publish_inbound_wake", record_publish)

        for _ in range(20):
            self._set_last_dispatch_age(db_conn, iid, 1000.0)
            self._dispatch(pool)

        assert len(calls) <= _MAX_DISPATCH_COUNT
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT dispatch_count, poisoned_at IS NOT NULL "
                "FROM inbound_messages WHERE id = %s",
                (iid,),
            )
            row = cur.fetchone()
        assert row == (_MAX_DISPATCH_COUNT, True)


class TestSelectPendingIds:
    def test_only_pending(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = insert_inbound_message(
            db_conn, aid, "hi", source="user", bus=event_bus, database=database
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (%s, %s, 'terminate', 'system')",
                (aid, "bye"),
            )
            cur.execute(
                "UPDATE inbound_messages SET status = 'done' "
                "WHERE kind = 'terminate' AND agent_id = %s",
                (aid,),
            )
        assert select_pending_ids(pool) >= {iid}


class TestDeadLetterStaleClaimed:
    """Stale 'claimed' rows of terminated owners are dead-lettered (flipped to
    'done') so a later resurrect cannot re-deliver them as fresh messages
    (Task #654)."""

    def test_old_claimed_of_terminated_owner_dead_lettered(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_claimed

        aid = _make_terminated_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = _insert_claimed_row(db_conn, aid, claim_age_s=2 * 86400)

        assert dead_letter_stale_claimed(pool, 86400.0, 7200.0) == 1
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (iid,))
            assert cur.fetchone() == ("done",)

    def test_fresh_claimed_of_terminated_owner_untouched(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """A young claim keeps the two-phase guarantee: if the agent is
        resurrected, boot reconcile still resets it to 'pending' for
        re-delivery (crash recovery)."""
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_claimed

        aid = _make_terminated_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = _insert_claimed_row(db_conn, aid, claim_age_s=60)

        assert dead_letter_stale_claimed(pool, 86400.0, 7200.0) == 0
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (iid,))
            assert cur.fetchone() == ("claimed",)

    def test_claimed_of_idling_and_running_owners_use_distinct_thresholds(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """Idling claims age out, while fresh idling and running claims stay."""
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_claimed

        stale_idling = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        stale_idling_row = _insert_claimed_row(db_conn, stale_idling, claim_age_s=7201)
        fresh_idling = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        fresh_idling_row = _insert_claimed_row(db_conn, fresh_idling, claim_age_s=3600)
        running = _make_running_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        running_row = _insert_claimed_row(db_conn, running, claim_age_s=10 * 86400)

        assert dead_letter_stale_claimed(pool, 86400.0, 7200.0) == 1
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM inbound_messages WHERE id = ANY(%s)",
                ([stale_idling_row, fresh_idling_row, running_row],),
            )
            assert dict(cur.fetchall()) == {
                stale_idling_row: "done",
                fresh_idling_row: "claimed",
                running_row: "claimed",
            }

    def test_null_claimed_at_falls_back_to_created_at(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """Rows claimed before the claimed_at column existed (2026-08-02) carry
        NULL claimed_at; created_at is the only age evidence, and it is stale
        by now — they must still be dead-lettered, not immortal."""
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_claimed

        aid = _make_terminated_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        old = _insert_claimed_row(db_conn, aid, claim_age_s=None, created_age_s=10 * 86400)
        fresh = _insert_claimed_row(db_conn, aid, claim_age_s=None, created_age_s=60)

        assert dead_letter_stale_claimed(pool, 86400.0, 7200.0) == 1
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM inbound_messages WHERE id IN (%s, %s)",
                (old, fresh),
            )
            assert dict(cur.fetchall()) == {old: "done", fresh: "claimed"}


class TestDeadLetterStalePendingResurrects:
    def test_only_old_pending_resurrects_are_dead_lettered(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_pending_resurrects

        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        old = _insert_pending_resurrect_row(db_conn, aid, age_s=2 * 86400)
        fresh = _insert_pending_resurrect_row(db_conn, aid, age_s=60)
        claimed = _insert_pending_resurrect_row(db_conn, aid, age_s=2 * 86400, status="claimed")
        done = _insert_pending_resurrect_row(db_conn, aid, age_s=2 * 86400, status="done")
        old_non_resurrect = _insert_pending_resurrect_row(
            db_conn, aid, age_s=2 * 86400, kind="chat"
        )

        assert dead_letter_stale_pending_resurrects(pool, 86400.0) == 1
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, claimed_at IS NOT NULL FROM inbound_messages "
                "WHERE id = ANY(%s)",
                ([old, fresh, claimed, done, old_non_resurrect],),
            )
            rows = {row[0]: row[1:] for row in cur.fetchall()}

        assert rows[old] == ("done", True)
        assert rows[fresh] == ("pending", False)
        assert rows[old_non_resurrect] == ("pending", False)
        assert rows[claimed] == ("claimed", False)
        assert rows[done] == ("done", False)


class TestDeadLetterStalePendingTerminated:
    def test_old_lifecycle_rows_of_terminated_owner_are_dead_lettered(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_pending_terminated

        aid = _make_terminated_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        rows = {
            _insert_pending_resurrect_row(db_conn, aid, age_s=2 * 86400, kind=kind)
            for kind in ("terminate", "system_note", "restart_completed")
        }

        assert dead_letter_stale_pending_terminated(pool, 86400.0) == 3
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, claimed_at IS NOT NULL FROM inbound_messages "
                "WHERE id = ANY(%s)",
                (list(rows),),
            )
            assert {row[0]: row[1:] for row in cur.fetchall()} == dict.fromkeys(
                rows, ("done", True)
            )

    def test_old_pending_chat_of_terminated_owner_is_untouched(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_pending_terminated

        aid = _make_terminated_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        row = _insert_pending_resurrect_row(db_conn, aid, age_s=2 * 86400, kind="chat")

        assert dead_letter_stale_pending_terminated(pool, 86400.0) == 0
        with db_conn.cursor() as cur:
            cur.execute("SELECT status, claimed_at FROM inbound_messages WHERE id = %s", (row,))
            assert cur.fetchone() == ("pending", None)

    def test_fresh_lifecycle_rows_of_terminated_owner_are_untouched(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_pending_terminated

        aid = _make_terminated_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        rows = [
            _insert_pending_resurrect_row(db_conn, aid, age_s=60, kind=kind)
            for kind in ("terminate", "system_note", "restart_completed")
        ]

        assert dead_letter_stale_pending_terminated(pool, 86400.0) == 0
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM inbound_messages WHERE id = ANY(%s)",
                (rows,),
            )
            assert dict(cur.fetchall()) == dict.fromkeys(rows, "pending")

    def test_old_lifecycle_rows_of_live_owner_are_untouched(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_pending_terminated

        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        rows = [
            _insert_pending_resurrect_row(db_conn, aid, age_s=2 * 86400, kind=kind)
            for kind in ("terminate", "system_note", "restart_completed")
        ]

        assert dead_letter_stale_pending_terminated(pool, 86400.0) == 0
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM inbound_messages WHERE id = ANY(%s)",
                (rows,),
            )
            assert dict(cur.fetchall()) == dict.fromkeys(rows, "pending")


class TestDeadLetterStalePendingChats:
    def test_old_pending_chat_of_terminated_owner_is_dead_lettered(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """Issue #2049: a chat that never claimed its terminated owner is
        archived once past the threshold instead of resurrecting it forever."""
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_pending_chats

        aid = _make_terminated_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        row = _insert_pending_resurrect_row(db_conn, aid, age_s=2 * 86400, kind="chat")

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
    ) -> None:
        """A recent pending chat is still a live resurrect candidate — the G4
        retry window must stay open until the threshold closes it."""
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_pending_chats

        aid = _make_terminated_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        row = _insert_pending_resurrect_row(db_conn, aid, age_s=60, kind="chat")

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
    ) -> None:
        """Live owners keep their pending chats: only terminated owners have no
        consumer, so the sweep never touches idling/running queues."""
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_pending_chats

        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        row = _insert_pending_resurrect_row(db_conn, aid, age_s=2 * 86400, kind="chat")

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
    ) -> None:
        """Lifecycle kinds keep their own sweep; this one is chat-only."""
        from services.wake.delivery_watchdog.daemon import dead_letter_stale_pending_chats

        aid = _make_terminated_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        row = _insert_pending_resurrect_row(db_conn, aid, age_s=2 * 86400, kind="terminate")

        assert dead_letter_stale_pending_chats(pool, 86400.0) == 0
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (row,))
            assert cur.fetchone() == ("pending",)
