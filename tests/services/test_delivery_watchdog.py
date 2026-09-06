"""`services.delivery_watchdog.daemon` — stale-pending-inbound selection + alerting.

`select_stale_pending` is the daemon's core predicate: chat inbounds still
`pending` past the threshold. `scan_once` adds the once-per-row-while-stuck
alert semantics (a row that flips pending -> claimed -> pending re-alerts).
"""

from __future__ import annotations

import time

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from services.delivery_watchdog.daemon import (
    dispatch_wakes,
    gc_alerted,
    persist_alerted,
    prune_alerted,
    scan_once,
    select_alerted_ids,
    select_pending_for_dispatch,
    select_pending_ids,
    select_stale_pending,
)
from shared import telemetry
from shared.config import settings
from shared.db import insert_inbound_message
from shared.redis_listener import RedisInboundListener

_THRESHOLD_S = 30.0
_DISPATCH_THRESHOLD_S = 1.0
_MAX_DISPATCH_COUNT = 5
_DISPATCH_BACKOFF_STEPS_S = [5.0, 30.0, 120.0, 300.0]


@pytest.fixture
def pool():
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


def _make_idling_agent(db: psycopg.Connection) -> int:
    """spawn_agent creates the agents_meta row (create_agent does not — that
    is the spawn path's job); the alert filter reads owner status, so tests
    spawn then park the agent 'idling' (same pattern as the heartbeat daemon
    tests)."""
    from tests.conftest import spawn_agent

    aid = spawn_agent(spawner="user")
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'idling' WHERE id = %s", (aid,))
    db.commit()
    return aid


def _make_running_agent(db: psycopg.Connection) -> int:
    from tests.conftest import spawn_agent

    aid = spawn_agent(spawner="user")
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (aid,))
    db.commit()
    return aid


def _insert_old_inbound(db: psycopg.Connection, agent_id: int, *, age_s: float) -> int:
    """Insert a chat inbound backdated `age_s` (timestamp-only UPDATE — the
    inbound table has no triggers on created_at). Returns the inbound id."""
    iid = insert_inbound_message(db, agent_id, "stale", source="user")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages SET created_at = now() - make_interval(secs => %s) "
            "WHERE id = %s",
            (age_s, iid),
        )
    db.commit()  # the pool's connections must see the backdate too
    return iid


class TestSelectStalePending:
    def test_returns_only_chat_pending_older_than_threshold(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        aid = _make_idling_agent(db_conn)
        old = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5)
        _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S - 10)  # fresh — excluded
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (%s, %s, 'terminate', 'system')",
                (aid, "bye"),
            )
            cur.execute(
                "UPDATE inbound_messages SET created_at = now() - make_interval(secs => %s) "
                "WHERE kind = 'terminate' AND agent_id = %s",
                (_THRESHOLD_S + 5, aid),
            )
        rows = select_stale_pending(pool, _THRESHOLD_S)
        assert [(r[0], r[2]) for r in rows] == [(old, f"#{aid}")] or [r[0] for r in rows] == [old]

    def test_claimed_or_done_rows_never_stale(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        aid = _make_idling_agent(db_conn)
        iid = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status = 'claimed' WHERE id = %s", (iid,))
        db_conn.commit()
        assert select_stale_pending(pool, _THRESHOLD_S) == []

    def test_empty_when_nothing_stale(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        aid = _make_idling_agent(db_conn)
        _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S - 1)
        assert select_stale_pending(pool, _THRESHOLD_S) == []

    def test_running_owner_queues_are_not_stalls(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """A chat inbound queued behind a long in-flight turn (owner
        status='running') is normal, not a delivery stall — the turn-end SELECT
        picks it up. Only waiting/terminal owners signal a real stall."""
        aid = _make_idling_agent(db_conn)
        _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (aid,))
        db_conn.commit()
        assert select_stale_pending(pool, _THRESHOLD_S) == []


class TestScanOnce:
    def test_alerts_each_stale_row_once_while_pending(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        aid = _make_idling_agent(db_conn)
        iid = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5)

        newly, alerted = scan_once(pool, _THRESHOLD_S, set())
        assert newly == 1
        assert alerted == {iid}

        # Second scan: still stale, already alerted -> no new alerts, no spam.
        newly, alerted = scan_once(pool, _THRESHOLD_S, alerted)
        assert newly == 0
        assert alerted == {iid}

    def test_row_that_leaves_pending_forgets_alert(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        aid = _make_idling_agent(db_conn)
        iid = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5)
        _, alerted = scan_once(pool, _THRESHOLD_S, set())

        with db_conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status = 'claimed' WHERE id = %s", (iid,))
        db_conn.commit()
        _, alerted2 = scan_once(pool, _THRESHOLD_S, alerted)
        assert alerted2 == set()

        # And if it comes back to pending (reconcile reset), it re-alerts.
        with db_conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status = 'pending' WHERE id = %s", (iid,))
        db_conn.commit()
        newly, _ = scan_once(pool, _THRESHOLD_S, alerted2)
        assert newly == 1

    def test_alert_writes_unified_event(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """The alert emits through the unified emitter: the canonical
        `events` row (telemetry/delivery_stalled). The legacy agent_events
        mirror is gone (tracker #898 term-alignment)."""
        aid = _make_idling_agent(db_conn)
        iid = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5)
        scan_once(pool, _THRESHOLD_S, set())
        # The emitter drains asynchronously (0.5s cadence) — flush() can race
        # the drain thread for the queue, so poll briefly for the line. The
        # event lives in the JSONL mirror (the PG events copy is a read-only
        # archive since the LGTM cutover, task #1197 close-C).
        import json as _json
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        from shared.paths import logs_dir

        def _stalled() -> dict[str, object] | None:
            telemetry.flush()
            day = _dt.now(_UTC).strftime("%Y%m%d")
            path = logs_dir() / f"events-{day}.jsonl"
            if not path.exists():
                return None
            for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                try:
                    obj = _json.loads(line)
                except ValueError:
                    continue
                if (
                    obj.get("event_name") == "delivery_stalled"
                    and obj.get("agent_id") == aid
                    and obj.get("category") == "telemetry"
                ):
                    return obj
            return None

        ev = None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            ev = _stalled()
            if ev is not None:
                break
            time.sleep(0.05)
        assert ev is not None
        assert ev["category"] == "telemetry"
        assert ev["event_name"] == "delivery_stalled"
        assert ev["level"] == "warning"
        attributes = ev["attributes"]
        assert isinstance(attributes, dict)
        assert attributes["inbound_id"] == iid


class TestSelectPendingForDispatch:
    def test_returns_pending_of_idling_owners_older_than_threshold(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """All kinds count (a lost wake strands terminate/restart too), any
        kind of stale pending of an idling owner is dispatched."""
        aid = _make_idling_agent(db_conn)
        old_chat = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 0.5)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (%s, %s, 'terminate', 'system') RETURNING id",
                (aid, "bye"),
            )
            term_row = cur.fetchone()
            assert term_row is not None
            term_id = term_row[0]
            cur.execute(
                "UPDATE inbound_messages SET created_at = now() - make_interval(secs => %s) "
                "WHERE id = %s",
                (_DISPATCH_THRESHOLD_S + 0.5, term_id),
            )
        db_conn.commit()
        rows = select_pending_for_dispatch(
            pool,
            _DISPATCH_THRESHOLD_S,
            _MAX_DISPATCH_COUNT,
            _DISPATCH_BACKOFF_STEPS_S,
        )
        assert {r[0] for r in rows} == {old_chat, term_id}
        assert all(r[1] == aid for r in rows)

    def test_fresh_rows_and_non_idling_owners_not_dispatched(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """Fresh rows (still within the dispatch threshold) and owners not in
        'idling' (running = mid-turn queue, terminated = its own controller)
        are left alone."""
        aid = _make_idling_agent(db_conn)
        _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S - 0.3)  # fresh
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (aid,))
        db_conn.commit()
        _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (aid,))
        db_conn.commit()
        _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        assert (
            select_pending_for_dispatch(
                pool,
                _DISPATCH_THRESHOLD_S,
                _MAX_DISPATCH_COUNT,
                _DISPATCH_BACKOFF_STEPS_S,
            )
            == []
        )

    def test_wake_suppression_excludes_until_expiry(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        aid = _make_idling_agent(db_conn)
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET wake_suppressed_until = now() + interval '1 hour', "
                "wake_suppress_reason = 'resurrect_failed' WHERE id = %s",
                (aid,),
            )
        db_conn.commit()

        assert (
            select_pending_for_dispatch(
                pool,
                _DISPATCH_THRESHOLD_S,
                _MAX_DISPATCH_COUNT,
                _DISPATCH_BACKOFF_STEPS_S,
            )
            == []
        )

        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET wake_suppressed_until = now() - interval '1 second' "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert select_pending_for_dispatch(
            pool,
            _DISPATCH_THRESHOLD_S,
            _MAX_DISPATCH_COUNT,
            _DISPATCH_BACKOFF_STEPS_S,
        ) == [(iid, aid)]


class TestDispatchWakes:
    def test_republishes_wake_per_stale_row(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """dispatch_wakes re-publishes one wake (payload = inbound id) per
        stale pending row of an idling owner — the lost-wake recovery."""
        import shared.db

        aid = _make_idling_agent(db_conn)
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 0.5)

        calls: list[tuple[int, str]] = []
        monkeypatch.setattr(
            shared.db,
            "publish_inbound_wake",
            lambda agent_id, payload: calls.append((agent_id, payload)) or True,  # pyright: ignore[reportUnknownArgumentType]
        )
        dispatched = dispatch_wakes(
            pool,
            _DISPATCH_THRESHOLD_S,
            _MAX_DISPATCH_COUNT,
            _DISPATCH_BACKOFF_STEPS_S,
        )
        assert dispatched == 1
        assert calls == [(aid, str(iid))]

    def test_publish_failure_does_not_raise(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing publish is logged, not raised — the alert path and the
        claim loop's 30s recheck remain as backstops."""
        import shared.db

        aid = _make_idling_agent(db_conn)
        _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 0.5)

        def boom(*_a, **_k) -> bool:
            return False

        monkeypatch.setattr(shared.db, "publish_inbound_wake", boom)  # pyright: ignore[reportUnknownArgumentType]
        assert (
            dispatch_wakes(
                pool,
                _DISPATCH_THRESHOLD_S,
                _MAX_DISPATCH_COUNT,
                _DISPATCH_BACKOFF_STEPS_S,
            )
            == 0
        )

    async def test_dispatched_wake_reaches_the_listener(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        aredis_inbound_listener: RedisInboundListener,
    ) -> None:
        """End-to-end: dispatch_wakes publishes on the agent's Redis channel,
        so a listener subscribed to it wakes immediately — the lost-wake window
        collapses from 30s to ~1 tick."""
        from shared.redis_listener import RedisInboundListener

        aid = _make_idling_agent(db_conn)
        _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 0.5)
        # The shared fixture listener is bound to the pseudo-agent-0 channel;
        # build one on THIS agent's channel.
        listener = RedisInboundListener(settings.data_plane.redis_url, aid)
        try:
            await listener.ensure_listening()
            dispatched = dispatch_wakes(
                pool,
                _DISPATCH_THRESHOLD_S,
                _MAX_DISPATCH_COUNT,
                _DISPATCH_BACKOFF_STEPS_S,
            )
            assert dispatched == 1
            await listener.wait_one(timeout=2.0)  # returns on the dispatched wake
        finally:
            await listener.close()


class TestDispatchBackoffAndPoison:
    @staticmethod
    def _dispatch(pool: ConnectionPool) -> int:
        return dispatch_wakes(
            pool,
            _DISPATCH_THRESHOLD_S,
            _MAX_DISPATCH_COUNT,
            _DISPATCH_BACKOFF_STEPS_S,
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
    ) -> None:
        aid = _make_idling_agent(db_conn)
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        calls: list[tuple[int, str]] = []

        def record_publish(agent_id: int, payload: str) -> bool:
            calls.append((agent_id, payload))
            return True

        monkeypatch.setattr("shared.db.publish_inbound_wake", record_publish)

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
    ) -> None:
        aid = _make_idling_agent(db_conn)
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)

        def accept_publish(_agent_id: int, _payload: str) -> bool:
            return True

        monkeypatch.setattr("shared.db.publish_inbound_wake", accept_publish)
        assert self._dispatch(pool) == 1

        assert (
            select_pending_for_dispatch(
                pool,
                _DISPATCH_THRESHOLD_S,
                _MAX_DISPATCH_COUNT,
                _DISPATCH_BACKOFF_STEPS_S,
            )
            == []
        )
        self._set_last_dispatch_age(db_conn, iid, 5.5)
        assert select_pending_for_dispatch(
            pool,
            _DISPATCH_THRESHOLD_S,
            _MAX_DISPATCH_COUNT,
            _DISPATCH_BACKOFF_STEPS_S,
        ) == [(iid, aid)]
        self._set_last_dispatch_age(db_conn, iid, 4.5)
        assert (
            select_pending_for_dispatch(
                pool,
                _DISPATCH_THRESHOLD_S,
                _MAX_DISPATCH_COUNT,
                _DISPATCH_BACKOFF_STEPS_S,
            )
            == []
        )
        self._set_last_dispatch_age(db_conn, iid, 1.5)
        assert select_pending_for_dispatch(
            pool,
            _DISPATCH_THRESHOLD_S,
            _MAX_DISPATCH_COUNT,
            [1.0],
        ) == [(iid, aid)]

    def test_publish_failure_does_not_increment_count(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        aid = _make_idling_agent(db_conn)
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)

        def fail_publish(*_args: object) -> bool:
            return False

        monkeypatch.setattr("shared.db.publish_inbound_wake", fail_publish)
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
    ) -> None:
        aid = _make_idling_agent(db_conn)
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)

        def publish_and_claim(agent_id: int, payload: str) -> bool:
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE inbound_messages SET status = 'claimed' WHERE id = %s",
                    (iid,),
                )
            db_conn.commit()
            return True

        monkeypatch.setattr("shared.db.publish_inbound_wake", publish_and_claim)
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
    ) -> None:
        import json
        from datetime import UTC, datetime

        from shared.paths import logs_dir

        aid = _make_idling_agent(db_conn)
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)

        def accept_publish(_agent_id: int, _payload: str) -> bool:
            return True

        monkeypatch.setattr("shared.db.publish_inbound_wake", accept_publish)

        for _ in range(_MAX_DISPATCH_COUNT):
            self._set_last_dispatch_age(db_conn, iid, 1000.0)
            assert self._dispatch(pool) == 1

        assert (
            select_pending_for_dispatch(
                pool,
                _DISPATCH_THRESHOLD_S,
                _MAX_DISPATCH_COUNT,
                _DISPATCH_BACKOFF_STEPS_S,
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

        def poisoned_events() -> list[dict[str, object]]:
            telemetry.flush()
            day = datetime.now(UTC).strftime("%Y%m%d")
            path = logs_dir() / f"events-{day}.jsonl"
            if not path.exists():
                return []
            events: list[dict[str, object]] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if (
                    event.get("event_name") == "delivery_poisoned"
                    and event.get("agent_id") == aid
                    and event.get("category") == "telemetry"
                ):
                    events.append(event)
            return events

        events: list[dict[str, object]] = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            events = poisoned_events()
            if events:
                break
            time.sleep(0.05)
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
        assert len(poisoned_events()) == 1

    def test_poisoned_row_is_not_dispatched_after_backoff(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        aid = _make_idling_agent(db_conn)
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

        def record_unexpected_publish(agent_id: int, payload: str) -> bool:
            calls.append((agent_id, payload))
            return True

        monkeypatch.setattr("shared.db.publish_inbound_wake", record_unexpected_publish)

        assert self._dispatch(pool) == 0
        assert calls == []

    def test_manual_reset_restores_dispatch(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        aid = _make_idling_agent(db_conn)
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

        def record_publish(agent_id: int, payload: str) -> bool:
            calls.append((agent_id, payload))
            return True

        monkeypatch.setattr("shared.db.publish_inbound_wake", record_publish)

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
    ) -> None:
        aid = _make_idling_agent(db_conn)
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        calls: list[tuple[int, str]] = []

        def record_publish(agent_id: int, payload: str) -> bool:
            calls.append((agent_id, payload))
            return True

        monkeypatch.setattr("shared.db.publish_inbound_wake", record_publish)

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
    def test_only_pending(self, db_conn: psycopg.Connection, pool: ConnectionPool) -> None:
        aid = _make_idling_agent(db_conn)
        iid = insert_inbound_message(db_conn, aid, "hi", source="user")
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


# ── Terminated-owner resurrect retry (Task #689 G4) ───────────────────────────


def _make_terminated_agent(db: psycopg.Connection) -> int:
    from tests.conftest import spawn_agent

    aid = spawn_agent(spawner="user")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'terminated', termination_source = 'exit' "
            "WHERE id = %s",
            (aid,),
        )
    db.commit()
    return aid


def _insert_claimed_row(
    db: psycopg.Connection,
    agent_id: int,
    *,
    claim_age_s: float | None,
    created_age_s: float | None = None,
) -> int:
    """Insert a 'claimed' chat inbound, backdating claimed_at (and optionally
    created_at) by the given ages. claimed_at NULL when claim_age_s is None —
    the pre-2026-08-02 shape (claimed before the column existed)."""
    created_age_s = (
        claim_age_s + 60 if created_age_s is None and claim_age_s is not None else created_age_s
    )
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages "
            "(agent_id, content, kind, source, status, claimed_at, created_at) "
            "VALUES (%s, %s, 'chat', 'user', 'claimed', "
            "now() - make_interval(secs => %s::double precision), "
            "now() - make_interval(secs => %s::double precision)) RETURNING id",
            (agent_id, "claimed msg", claim_age_s, created_age_s),
        )
        iid = cur.fetchone()[0]  # type: ignore[index]
    db.commit()
    return iid


class TestDeadLetterStaleClaimed:
    """Stale 'claimed' rows of terminated owners are dead-lettered (flipped to
    'done') so a later resurrect cannot re-deliver them as fresh messages
    (Task #654)."""

    def test_old_claimed_of_terminated_owner_dead_lettered(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        from services.delivery_watchdog.daemon import dead_letter_stale_claimed

        aid = _make_terminated_agent(db_conn)
        iid = _insert_claimed_row(db_conn, aid, claim_age_s=2 * 86400)

        assert dead_letter_stale_claimed(pool, 86400.0, 7200.0) == 1
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (iid,))
            assert cur.fetchone() == ("done",)

    def test_fresh_claimed_of_terminated_owner_untouched(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """A young claim keeps the two-phase guarantee: if the agent is
        resurrected, boot reconcile still resets it to 'pending' for
        re-delivery (crash recovery)."""
        from services.delivery_watchdog.daemon import dead_letter_stale_claimed

        aid = _make_terminated_agent(db_conn)
        iid = _insert_claimed_row(db_conn, aid, claim_age_s=60)

        assert dead_letter_stale_claimed(pool, 86400.0, 7200.0) == 0
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (iid,))
            assert cur.fetchone() == ("claimed",)

    def test_claimed_of_idling_and_running_owners_use_distinct_thresholds(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """Idling claims age out, while fresh idling and running claims stay."""
        from services.delivery_watchdog.daemon import dead_letter_stale_claimed

        stale_idling = _make_idling_agent(db_conn)
        stale_idling_row = _insert_claimed_row(db_conn, stale_idling, claim_age_s=7201)
        fresh_idling = _make_idling_agent(db_conn)
        fresh_idling_row = _insert_claimed_row(db_conn, fresh_idling, claim_age_s=3600)
        running = _make_running_agent(db_conn)
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
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """Rows claimed before the claimed_at column existed (2026-08-02) carry
        NULL claimed_at; created_at is the only age evidence, and it is stale
        by now — they must still be dead-lettered, not immortal."""
        from services.delivery_watchdog.daemon import dead_letter_stale_claimed

        aid = _make_terminated_agent(db_conn)
        old = _insert_claimed_row(db_conn, aid, claim_age_s=None, created_age_s=10 * 86400)
        fresh = _insert_claimed_row(db_conn, aid, claim_age_s=None, created_age_s=60)

        assert dead_letter_stale_claimed(pool, 86400.0, 7200.0) == 1
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM inbound_messages WHERE id IN (%s, %s)",
                (old, fresh),
            )
            assert dict(cur.fetchall()) == {old: "done", fresh: "claimed"}


def _insert_pending_resurrect_row(
    db: psycopg.Connection,
    agent_id: int,
    *,
    age_s: float,
    status: str = "pending",
    kind: str = "resurrect",
) -> int:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, status, created_at) "
            "VALUES (%s, '', %s, 'system', %s, "
            "now() - make_interval(secs => %s::double precision)) RETURNING id",
            (agent_id, kind, status, age_s),
        )
        inbound_id = cur.fetchone()[0]  # type: ignore[index]
    db.commit()
    return inbound_id


class TestDeadLetterStalePendingResurrects:
    def test_only_old_pending_resurrects_are_dead_lettered(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        from services.delivery_watchdog.daemon import dead_letter_stale_pending_resurrects

        aid = _make_idling_agent(db_conn)
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
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        from services.delivery_watchdog.daemon import dead_letter_stale_pending_terminated

        aid = _make_terminated_agent(db_conn)
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
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        from services.delivery_watchdog.daemon import dead_letter_stale_pending_terminated

        aid = _make_terminated_agent(db_conn)
        row = _insert_pending_resurrect_row(db_conn, aid, age_s=2 * 86400, kind="chat")

        assert dead_letter_stale_pending_terminated(pool, 86400.0) == 0
        with db_conn.cursor() as cur:
            cur.execute("SELECT status, claimed_at FROM inbound_messages WHERE id = %s", (row,))
            assert cur.fetchone() == ("pending", None)

    def test_fresh_lifecycle_rows_of_terminated_owner_are_untouched(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        from services.delivery_watchdog.daemon import dead_letter_stale_pending_terminated

        aid = _make_terminated_agent(db_conn)
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
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        from services.delivery_watchdog.daemon import dead_letter_stale_pending_terminated

        aid = _make_idling_agent(db_conn)
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


class TestSelectTerminatedOwnersWithPending:
    def test_force_fence_excludes_older_chat_but_accepts_newer_chat(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """The selector uses the monotonic explicit-kill fence in addition to
        wall-clock status time: old queued work stays dead, later work wakes."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
        old_chat_id = insert_inbound_message(db_conn, aid, "before force", source="user")
        fence_id = insert_inbound_message(
            db_conn,
            aid,
            "",
            source="user",
            kind="terminate",
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET last_force_terminate_inbound_id = %s WHERE id = %s",
                (fence_id, aid),
            )
        db_conn.commit()

        assert old_chat_id < fence_id
        assert select_terminated_owners_with_pending(pool) == []

        new_chat_id = insert_inbound_message(db_conn, aid, "after force", source="user")
        assert new_chat_id > fence_id
        assert select_terminated_owners_with_pending(pool) == [(aid, new_chat_id)]

    def test_ignores_pending_chat_that_predates_latest_termination(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """A user's explicit kill wins over mail already waiting when they
        killed the agent; that old row must not immediately undo the kill."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_idling_agent(db_conn)
        iid = insert_inbound_message(db_conn, aid, "already waiting", source="user")
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

        assert select_terminated_owners_with_pending(pool) == []

    def test_returns_pending_chat_created_after_latest_termination(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """A new chat sent after termination preserves the existing contract:
        delivery to a dead agent wakes it automatically."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
        iid = insert_inbound_message(db_conn, aid, "new request", source="user")
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages "
                "SET created_at = (SELECT status_changed_at FROM agents_meta WHERE id = %s) "
                "                 + interval '1 second' "
                "WHERE id = %s",
                (aid, iid),
            )
        db_conn.commit()

        assert select_terminated_owners_with_pending(pool) == [(aid, iid)]

    def test_returns_terminated_owners_with_pending_chat(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
        iid = insert_inbound_message(db_conn, aid, "hello?", source="user")

        assert select_terminated_owners_with_pending(pool) == [(aid, iid)]

    def test_deduplicates_per_agent(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """250 dead letters for one agent mean ONE resurrect, not 250."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
        iids: list[int] = []
        for _ in range(3):
            iids.append(insert_inbound_message(db_conn, aid, "hello?", source="user"))

        assert select_terminated_owners_with_pending(pool) == [(aid, min(iids))]

    def test_ignores_live_owners_and_non_chat_kinds(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        live = _make_idling_agent(db_conn)  # idling owner — not a resurrect case
        insert_inbound_message(db_conn, live, "hi", source="user")
        dead = _make_terminated_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (%s, %s, 'restart', 'system')",
                (dead, ""),
            )
        db_conn.commit()

        assert select_terminated_owners_with_pending(pool) == []

    def test_claimed_chat_is_not_retried(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
        iid = insert_inbound_message(db_conn, aid, "hello?", source="user")
        with db_conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status = 'claimed' WHERE id = %s", (iid,))
        db_conn.commit()

        assert select_terminated_owners_with_pending(pool) == []

    def test_wake_suppression_excludes_until_expiry(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
        iid = insert_inbound_message(db_conn, aid, "queued during suppression", source="agent:1")
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET wake_suppressed_until = now() + interval '1 hour', "
                "wake_suppress_reason = 'resurrect_failed' WHERE id = %s",
                (aid,),
            )
        db_conn.commit()

        assert select_terminated_owners_with_pending(pool) == []

        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET wake_suppressed_until = now() - interval '1 second' "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert select_terminated_owners_with_pending(pool) == [(aid, iid)]


class TestResurrectRetry:
    async def test_repeated_terminated_results_suppress_and_emit_once(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import asyncio

        import ops.ops_lifecycle as ol
        import services.delivery_watchdog.daemon as dw
        from shared.agents import AgentStatus

        aid = _make_terminated_agent(db_conn)
        trigger_id = insert_inbound_message(db_conn, aid, "hello?", source="user")
        calls: list[int] = []

        async def _still_terminated(
            agent_id: int,
            *,
            trigger_inbound_id: int,
            trigger_inbound_kind: str,
        ) -> AgentStatus:
            assert trigger_inbound_id == trigger_id
            assert trigger_inbound_kind == "chat"
            calls.append(agent_id)
            return AgentStatus.TERMINATED

        emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def _record_emit(*args: object, **kwargs: object) -> None:
            emitted.append((args, kwargs))

        monkeypatch.setattr(ol, "resurrect_if_terminated", _still_terminated)
        monkeypatch.setattr(dw.telemetry, "emit", _record_emit)
        monkeypatch.setattr(settings.daemon, "delivery_watchdog_resurrect_fail_before_suppress", 5)
        monkeypatch.setattr(settings.daemon, "delivery_watchdog_suppress_base_seconds", 1800.0)
        monkeypatch.setattr(settings.daemon, "delivery_watchdog_suppress_max_seconds", 86400.0)
        dw._resurrect_failures.clear()
        dw._resurrect_suppressions.clear()
        dw._last_resurrect_attempt.clear()
        dw._resurrect_tasks.clear()

        for _ in range(5):
            await dw._resurrect_one(pool, aid, trigger_id)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT wake_suppress_reason, "
                "EXTRACT(EPOCH FROM (wake_suppressed_until - clock_timestamp())) "
                "FROM agents_meta WHERE id = %s",
                (aid,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "resurrect_failed"
        assert 1795.0 < float(row[1]) <= 1800.0
        assert calls == [aid] * 5
        assert len(emitted) == 1
        assert emitted[0][0] == ("telemetry", "delivery_wake_suppressed")
        assert emitted[0][1]["agent_id"] == aid
        assert emitted[0][1]["attributes"] == {
            "consecutive_failures": 5,
            "suppress_seconds": 1800.0,
            "suppress_count": 1,
            "reason": "resurrect_failed",
        }

        # The durable selector, not the in-memory cooldown, prevents later
        # watchdog ticks from scheduling another attempt during suppression.
        dw._last_resurrect_attempt.clear()
        dw._maybe_spawn_resurrects(pool, max_per_tick=5)
        await asyncio.sleep(0)
        assert dw._resurrect_tasks == {}
        assert calls == [aid] * 5

    async def test_success_resets_failure_and_suppression_escalation_counts(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ops.ops_lifecycle as ol
        import services.delivery_watchdog.daemon as dw
        from shared.agents import AgentStatus

        aid = _make_terminated_agent(db_conn)
        trigger_id = insert_inbound_message(db_conn, aid, "hello?", source="user")

        async def _resurrected(*_args: object, **_kwargs: object) -> AgentStatus:
            return AgentStatus.IDLING

        monkeypatch.setattr(ol, "resurrect_if_terminated", _resurrected)
        dw._resurrect_failures[aid] = 4
        dw._resurrect_suppressions[aid] = 3

        await dw._resurrect_one(pool, aid, trigger_id)

        assert aid not in dw._resurrect_failures
        assert aid not in dw._resurrect_suppressions

    async def test_expired_suppression_escalates_again_with_bounded_backoff(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ops.ops_lifecycle as ol
        import services.delivery_watchdog.daemon as dw
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending
        from shared.agents import AgentStatus

        aid = _make_terminated_agent(db_conn)
        trigger_id = insert_inbound_message(db_conn, aid, "hello?", source="user")

        async def _still_terminated(*_args: object, **_kwargs: object) -> AgentStatus:
            return AgentStatus.TERMINATED

        def _ignore_emit(*_args: object, **_kwargs: object) -> None:
            pass

        monkeypatch.setattr(ol, "resurrect_if_terminated", _still_terminated)
        monkeypatch.setattr(dw.telemetry, "emit", _ignore_emit)
        monkeypatch.setattr(settings.daemon, "delivery_watchdog_resurrect_fail_before_suppress", 1)
        monkeypatch.setattr(settings.daemon, "delivery_watchdog_suppress_base_seconds", 10.0)
        monkeypatch.setattr(settings.daemon, "delivery_watchdog_suppress_max_seconds", 15.0)
        dw._resurrect_failures.clear()
        dw._resurrect_suppressions.clear()

        await dw._resurrect_one(pool, aid, trigger_id)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET wake_suppressed_until = now() - interval '1 second' "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert select_terminated_owners_with_pending(pool) == [(aid, trigger_id)]

        await dw._resurrect_one(pool, aid, trigger_id)
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM (wake_suppressed_until - clock_timestamp())) "
                "FROM agents_meta WHERE id = %s",
                (aid,),
            )
            row = cur.fetchone()
        assert row is not None
        assert 14.0 < float(row[0]) <= 15.0
        assert dw._resurrect_suppressions[aid] == 2

    async def test_failed_resurrect_cleans_in_flight_and_enters_cooldown(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed RPC is complete work: its task key is removed and its real
        finally timestamp prevents the next tick from retrying immediately."""
        import asyncio

        import ops.ops_lifecycle as ol
        import services.delivery_watchdog.daemon as dw

        aid = _make_terminated_agent(db_conn)
        insert_inbound_message(db_conn, aid, "hello?", source="user")
        calls: list[int] = []

        async def _fail(
            aid_: int,
            *,
            trigger_inbound_id: int | None = None,
            trigger_inbound_kind: str | None = None,
        ) -> str:
            calls.append(aid_)
            raise RuntimeError("runner unavailable")

        monkeypatch.setattr(ol, "resurrect_if_terminated", _fail)
        dw._last_resurrect_attempt.clear()
        dw._resurrect_tasks.clear()

        dw._maybe_spawn_resurrects(pool, max_per_tick=5)
        await asyncio.gather(*list(dw._resurrect_tasks.values()))
        await asyncio.sleep(0)
        assert dw._resurrect_tasks == {}
        assert aid in dw._last_resurrect_attempt

        dw._maybe_spawn_resurrects(pool, max_per_tick=5)
        await asyncio.sleep(0)
        assert calls == [aid]

    async def test_cancelled_resurrect_cleans_in_flight_and_enters_cooldown(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancellation cannot leave a permanent in-flight key or bypass the
        retry cooldown once the RPC body has started."""
        import asyncio

        import ops.ops_lifecycle as ol
        import services.delivery_watchdog.daemon as dw

        aid = _make_terminated_agent(db_conn)
        insert_inbound_message(db_conn, aid, "hello?", source="user")
        started = asyncio.Event()

        async def _block(
            aid_: int,
            *,
            trigger_inbound_id: int | None = None,
            trigger_inbound_kind: str | None = None,
        ) -> str:
            started.set()
            await asyncio.Event().wait()
            return "terminated"

        monkeypatch.setattr(ol, "resurrect_if_terminated", _block)
        dw._last_resurrect_attempt.clear()
        dw._resurrect_tasks.clear()

        dw._maybe_spawn_resurrects(pool, max_per_tick=5)
        await started.wait()
        task = dw._resurrect_tasks[aid]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

        assert dw._resurrect_tasks == {}
        assert aid in dw._last_resurrect_attempt
        dw._maybe_spawn_resurrects(pool, max_per_tick=5)
        assert dw._resurrect_tasks == {}

    async def test_in_flight_owner_does_not_consume_next_tick_cap(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With cap=1, owner A still in flight is skipped before admission
        accounting, so the following tick can fairly admit owner B."""
        import asyncio

        import ops.ops_lifecycle as ol
        import services.delivery_watchdog.daemon as dw

        dead_a = _make_terminated_agent(db_conn)
        dead_b = _make_terminated_agent(db_conn)
        for aid in (dead_a, dead_b):
            insert_inbound_message(db_conn, aid, "hello?", source="user")
        started: set[int] = set()
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def _block(
            aid: int,
            *,
            trigger_inbound_id: int | None = None,
            trigger_inbound_kind: str | None = None,
        ) -> str:
            started.add(aid)
            if len(started) == 2:
                both_started.set()
            await release.wait()
            return "terminated"

        monkeypatch.setattr(ol, "resurrect_if_terminated", _block)
        dw._last_resurrect_attempt.clear()
        dw._resurrect_tasks.clear()

        try:
            dw._maybe_spawn_resurrects(pool, max_per_tick=1)
            await asyncio.sleep(0)
            assert started == {dead_a}

            dw._maybe_spawn_resurrects(pool, max_per_tick=1)
            await both_started.wait()
            assert started == {dead_a, dead_b}
            assert set(dw._resurrect_tasks) == {dead_a, dead_b}
        finally:
            release.set()
            await asyncio.gather(*list(dw._resurrect_tasks.values()), return_exceptions=True)
            await asyncio.sleep(0)

    async def test_in_flight_resurrect_is_not_enqueued_again_on_next_tick(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slow resurrect RPC spans many watchdog ticks; every later tick
        must reuse its in-flight attempt instead of building a task herd."""
        import asyncio

        import ops.ops_lifecycle as ol
        import services.delivery_watchdog.daemon as dw

        aid = _make_terminated_agent(db_conn)
        iid = insert_inbound_message(db_conn, aid, "hello?", source="user")
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[tuple[int, int | None, str | None]] = []

        async def slow_resurrect(
            aid_: int,
            *,
            trigger_inbound_id: int | None = None,
            trigger_inbound_kind: str | None = None,
        ) -> str:
            calls.append((aid_, trigger_inbound_id, trigger_inbound_kind))
            started.set()
            await release.wait()
            return "terminated"

        monkeypatch.setattr(ol, "resurrect_if_terminated", slow_resurrect)
        dw._last_resurrect_attempt.clear()
        dw._resurrect_tasks.clear()

        try:
            dw._maybe_spawn_resurrects(pool, max_per_tick=5)
            await started.wait()
            for _ in range(3):
                dw._maybe_spawn_resurrects(pool, max_per_tick=5)
            await asyncio.sleep(0)

            assert calls == [(aid, iid, "chat")]
            assert len(dw._resurrect_tasks) == 1
        finally:
            release.set()
            await asyncio.gather(*list(dw._resurrect_tasks.values()), return_exceptions=True)
            dw._resurrect_tasks.clear()

    async def test_cooldown_and_cap_bound_retries(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-agent 60s cooldown + per-tick cap: a dead-letter pile for an
        unreachable home machine drains over ticks, never as a burst."""
        import asyncio

        import ops.ops_lifecycle as ol
        import services.delivery_watchdog.daemon as dw

        dead_a = _make_terminated_agent(db_conn)
        dead_b = _make_terminated_agent(db_conn)
        for aid in (dead_a, dead_b):
            insert_inbound_message(db_conn, aid, "hello?", source="user")

        calls: list[int] = []
        hold = False
        release = asyncio.Event()

        async def fake_resurrect(
            aid: int,
            *,
            trigger_inbound_id: int | None = None,
            trigger_inbound_kind: str | None = None,
        ) -> str:
            calls.append(aid)
            assert trigger_inbound_id is not None
            if hold:
                await release.wait()
            return "terminated"

        monkeypatch.setattr(ol, "resurrect_if_terminated", fake_resurrect)
        dw._last_resurrect_attempt.clear()
        dw._resurrect_tasks.clear()

        # First pass completes both attempts and naturally stamps cooldown.
        dw._maybe_spawn_resurrects(pool, max_per_tick=5)
        await asyncio.gather(*list(dw._resurrect_tasks.values()))
        await asyncio.sleep(0)  # run task done callbacks
        assert len(dw._resurrect_tasks) == 0
        assert set(calls) == {dead_a, dead_b}

        # Second pass inside the real cooldown: neither spawns again.
        dw._maybe_spawn_resurrects(pool, max_per_tick=5)
        await asyncio.sleep(0)
        assert len(dw._resurrect_tasks) == 0
        assert len(calls) == 2

        # Cap: once cooldown is cleared, max_per_tick=1 admits only one owner.
        hold = True
        dw._last_resurrect_attempt.clear()
        dw._maybe_spawn_resurrects(pool, max_per_tick=1)
        await asyncio.sleep(0)
        assert len(dw._resurrect_tasks) == 1
        release.set()
        await asyncio.gather(*list(dw._resurrect_tasks.values()), return_exceptions=True)
        await asyncio.sleep(0)
        assert len(dw._resurrect_tasks) == 0

    async def test_resurrect_one_runs_and_records_attempt(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_resurrect_one calls the lazy-imported resurrect_if_terminated and
        stamps the per-agent attempt clock (drives the 60s cooldown)."""
        import time

        import ops.ops_lifecycle as ol
        import services.delivery_watchdog.daemon as dw

        aid = _make_terminated_agent(db_conn)
        trigger_id = insert_inbound_message(db_conn, aid, "hello?", source="user")
        calls: list[tuple[int, int | None, str | None]] = []

        async def fake(
            aid_: int,
            *,
            trigger_inbound_id: int | None = None,
            trigger_inbound_kind: str | None = None,
        ) -> str:
            calls.append((aid_, trigger_inbound_id, trigger_inbound_kind))
            return "terminated"

        monkeypatch.setattr(ol, "resurrect_if_terminated", fake)
        dw._last_resurrect_attempt.clear()

        await dw._resurrect_one(pool, aid, trigger_id)

        assert calls == [(aid, trigger_id, "chat")]
        assert dw._last_resurrect_attempt[aid] >= time.monotonic() - 5


class TestAlertDedupPersistence:
    """Task #945: the once-per-row alert set survives daemon restarts via the
    `delivery_watchdog_alerted` table — a restart must not re-report every
    still-stalled inbound, and a row that leaves pending must still be
    forgotten (so the pending -> claimed -> pending flip re-alerts)."""

    def test_persist_then_reload_seeds_alerted_set(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        aid = _make_idling_agent(db_conn)
        iid = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5)

        # First daemon life: alert once, persist the delta.
        newly, alerted = scan_once(pool, _THRESHOLD_S, set())
        assert newly == 1
        persist_alerted(pool, alerted - set())

        # "Restart": a fresh in-memory set seeded from the table must dedup
        # the still-stalled inbound — no re-report burst.
        reloaded = select_alerted_ids(pool)
        assert reloaded == {iid}
        newly, _ = scan_once(pool, _THRESHOLD_S, reloaded)
        assert newly == 0

    def test_reload_roundtrip_persists_only_given_ids(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        # FK -> inbound_messages: only real inbound ids can be persisted.
        aid = _make_idling_agent(db_conn)
        iid_a = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5)
        iid_b = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5)
        persist_alerted(pool, {iid_a, iid_b})
        assert select_alerted_ids(pool) == {iid_a, iid_b}
        # A second persist of the same ids is a no-op (ON CONFLICT DO NOTHING).
        persist_alerted(pool, {iid_a, iid_b})
        assert select_alerted_ids(pool) == {iid_a, iid_b}

    def test_prune_forgets_rows_that_left_pending(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        aid = _make_idling_agent(db_conn)
        iid = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5)
        _, alerted = scan_once(pool, _THRESHOLD_S, set())
        persist_alerted(pool, alerted)

        # Inbound gets claimed (delivered): the row leaves pending.
        with db_conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status = 'claimed' WHERE id = %s", (iid,))
        db_conn.commit()
        # Same scan semantics as the daemon: scan_once prunes `alerted` in
        # place, so snapshot before the call, then prune the delta.
        prev_alerted = set(alerted)
        _, alerted2 = scan_once(pool, _THRESHOLD_S, alerted)
        prune_alerted(pool, prev_alerted - alerted2)
        assert select_alerted_ids(pool) == set()

        # And the flip back to pending re-alerts, exactly as with memory alone.
        with db_conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status = 'pending' WHERE id = %s", (iid,))
        db_conn.commit()
        newly, alerted3 = scan_once(pool, _THRESHOLD_S, set())
        assert newly == 1
        assert alerted3 == {iid}

    def test_gc_removes_old_rows(self, db_conn: psycopg.Connection, pool: ConnectionPool) -> None:
        aid = _make_idling_agent(db_conn)
        iid = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5)
        persist_alerted(pool, {iid})
        # Backdate the row beyond the TTL (2h) — as if it were alerted long ago
        # and the inbound left pending while the daemon was down.
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE delivery_watchdog_alerted "
                "SET alerted_at = now() - make_interval(hours => 24) "
                "WHERE inbound_id = %s",
                (iid,),
            )
        db_conn.commit()
        removed = gc_alerted(pool, 2 * 3600.0)
        assert removed == 1
        assert select_alerted_ids(pool) == set()

    def test_prune_empty_and_persist_empty_are_noops(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        prune_alerted(pool, set())
        persist_alerted(pool, set())
        assert select_alerted_ids(pool) == set()
