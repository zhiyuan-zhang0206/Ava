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

from base import telemetry
from base.config import settings
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from base.events.live.redis_listener import RedisInboundListener
from services.delivery_watchdog.daemon import (
    dispatch_wakes,
    scan_once,
    select_pending_for_dispatch,
    select_pending_ids,
    select_stale_pending,
)

_THRESHOLD_S = 30.0
_DISPATCH_THRESHOLD_S = 1.0
_MAX_DISPATCH_COUNT = 5
_DISPATCH_BACKOFF_STEPS_S = [5.0, 30.0, 120.0, 300.0]
_HOST_STALENESS_S = 120.0


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
    from tests.fixtures.units import spawn_agent

    aid = spawn_agent(spawner="user")
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'idling' WHERE id = %s", (aid,))
    db.commit()
    return aid


def _make_running_agent(db: psycopg.Connection) -> int:
    from tests.fixtures.units import spawn_agent

    aid = spawn_agent(spawner="user")
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (aid,))
    db.commit()
    return aid


def _insert_old_inbound(db: psycopg.Connection, agent_id: int, *, age_s: float) -> int:
    """Insert a chat inbound backdated `age_s` (timestamp-only UPDATE — the
    inbound table has no triggers on created_at). Returns the inbound id."""
    iid = insert_inbound_message(
        db,
        agent_id,
        "stale",
        source="user",
        bus=EventBus.from_settings(),
        database=Database.from_settings(),
    )
    with db.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages SET created_at = now() - make_interval(secs => %s) "
            "WHERE id = %s",
            (age_s, iid),
        )
    db.commit()  # the pool's connections must see the backdate too
    return iid


def _set_host_verdict(
    db: psycopg.Connection,
    *,
    online: bool = True,
    agent_host: bool = True,
    age_s: float = 0.0,
    machine: str | None = None,
) -> None:
    """Upsert the `machine_probe` verdict the dispatch host gate reads."""
    from base.cluster.machine import machine_name

    name = machine or machine_name()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO machine_probe (machine_name, online, agent_host_online, "
            "consecutive_failures, last_probe_at) "
            "VALUES (%s, %s, %s, 0, now() - make_interval(secs => %s)) "
            "ON CONFLICT (machine_name) DO UPDATE SET online = EXCLUDED.online, "
            "agent_host_online = EXCLUDED.agent_host_online, "
            "consecutive_failures = EXCLUDED.consecutive_failures, "
            "last_probe_at = EXCLUDED.last_probe_at",
            (name, online, agent_host, age_s),
        )
    db.commit()


@pytest.fixture(autouse=True)
def _healthy_host_verdict(db_conn: psycopg.Connection) -> None:
    """Normal dispatch condition: a fresh reachable machine with a live host.

    Host-gate tests override the verdict inside the test body."""
    _set_host_verdict(db_conn)


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
        # event lives in the JSONL mirror (the PG events copy was retired at
        # the LGTM cutover, task #1197 close-C, and dropped with the archive
        # cleanup, task #1281/#1823).
        import json as _json
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        from base.paths import logs_dir

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


class TestDispatchWakes:
    def test_republishes_wake_per_stale_row(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """dispatch_wakes re-publishes one wake (payload = inbound id) per
        stale pending row of an idling owner — the lost-wake recovery."""
        import base.db

        aid = _make_idling_agent(db_conn)
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 0.5)

        calls: list[tuple[int, str]] = []
        monkeypatch.setattr(
            base.db,
            "publish_inbound_wake",
            lambda _db, _bus, agent_id, payload: calls.append((agent_id, payload)) or True,  # pyright: ignore[reportUnknownArgumentType]
        )
        dispatched = dispatch_wakes(
            pool,
            database,
            event_bus,
            _DISPATCH_THRESHOLD_S,
            _MAX_DISPATCH_COUNT,
            _DISPATCH_BACKOFF_STEPS_S,
            _HOST_STALENESS_S,
        )
        assert dispatched == 1
        assert calls == [(aid, str(iid))]

    def test_publish_failure_does_not_raise(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """A failing publish is logged, not raised — the alert path and the
        claim loop's 30s recheck remain as backstops."""
        import base.db

        aid = _make_idling_agent(db_conn)
        _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 0.5)

        def boom(_db: object, _bus: object, *_a, **_k) -> bool:
            return False

        monkeypatch.setattr(base.db, "publish_inbound_wake", boom)  # pyright: ignore[reportUnknownArgumentType]
        assert (
            dispatch_wakes(
                pool,
                database,
                event_bus,
                _DISPATCH_THRESHOLD_S,
                _MAX_DISPATCH_COUNT,
                _DISPATCH_BACKOFF_STEPS_S,
                _HOST_STALENESS_S,
            )
            == 0
        )

    async def test_dispatched_wake_reaches_the_listener(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        aredis_inbound_listener: RedisInboundListener,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """End-to-end: dispatch_wakes publishes on the agent's Redis channel,
        so a listener subscribed to it wakes immediately — the lost-wake window
        collapses from 30s to ~1 tick."""
        from base.events.live.redis_listener import RedisInboundListener

        aid = _make_idling_agent(db_conn)
        _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 0.5)
        # The shared fixture listener is bound to the pseudo-agent-0 channel;
        # build one on THIS agent's channel.
        listener = RedisInboundListener(settings.data_plane.redis_url, aid)
        try:
            await listener.ensure_listening()
            dispatched = dispatch_wakes(
                pool,
                database,
                event_bus,
                _DISPATCH_THRESHOLD_S,
                _MAX_DISPATCH_COUNT,
                _DISPATCH_BACKOFF_STEPS_S,
                _HOST_STALENESS_S,
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
    ) -> None:
        aid = _make_idling_agent(db_conn)
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
    ) -> None:
        aid = _make_idling_agent(db_conn)
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
    ) -> None:
        aid = _make_idling_agent(db_conn)
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
    ) -> None:
        aid = _make_idling_agent(db_conn)
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
    ) -> None:
        import json
        from datetime import UTC, datetime

        from base.paths import logs_dir

        aid = _make_idling_agent(db_conn)
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
    ) -> None:
        aid = _make_idling_agent(db_conn)
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
    ) -> None:
        aid = _make_idling_agent(db_conn)
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


# ── Terminated-owner resurrect retry (Task #689 G4) ───────────────────────────


def _make_terminated_agent(db: psycopg.Connection) -> int:
    from tests.fixtures.units import spawn_agent

    aid = spawn_agent(spawner="user")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'terminated', termination_source = 'exit' "
            "WHERE id = %s",
            (aid,),
        )
    db.commit()
    return aid


def _make_reaped_crash_agent(db: psycopg.Connection) -> int:
    """A `terminated` row the SYSTEM reaped after a crash: reaper source plus
    the retained crash marker (task #3617's relaxed-trigger population)."""
    aid = _make_terminated_agent(db)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET termination_source = 'reaper', "
            "last_turn_fatal_at = now() - interval '1 hour' WHERE id = %s",
            (aid,),
        )
    db.commit()
    return aid


def _backdate_chat_before_termination(db: psycopg.Connection, aid: int, iid: int) -> None:
    """Move a chat's created_at just before the row's current status epoch —
    the "already waiting when the death happened" shape (6260's leftovers)."""
    with db.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages SET created_at = "
            "(SELECT status_changed_at FROM agents_meta WHERE id = %s) - interval '1 second' "
            "WHERE id = %s",
            (aid, iid),
        )
    db.commit()


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


class TestDeadLetterStalePendingChats:
    def test_old_pending_chat_of_terminated_owner_is_dead_lettered(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """Issue #2049: a chat that never claimed its terminated owner is
        archived once past the threshold instead of resurrecting it forever."""
        from services.delivery_watchdog.daemon import dead_letter_stale_pending_chats

        aid = _make_terminated_agent(db_conn)
        row = _insert_pending_resurrect_row(db_conn, aid, age_s=2 * 86400, kind="chat")

        assert dead_letter_stale_pending_chats(pool, 86400.0) == 1
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status, claimed_at IS NOT NULL FROM inbound_messages WHERE id = %s",
                (row,),
            )
            assert cur.fetchone() == ("done", True)

    def test_fresh_pending_chat_of_terminated_owner_is_untouched(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """A recent pending chat is still a live resurrect candidate — the G4
        retry window must stay open until the threshold closes it."""
        from services.delivery_watchdog.daemon import dead_letter_stale_pending_chats

        aid = _make_terminated_agent(db_conn)
        row = _insert_pending_resurrect_row(db_conn, aid, age_s=60, kind="chat")

        assert dead_letter_stale_pending_chats(pool, 86400.0) == 0
        with db_conn.cursor() as cur:
            cur.execute("SELECT status, claimed_at FROM inbound_messages WHERE id = %s", (row,))
            assert cur.fetchone() == ("pending", None)

    def test_old_pending_chat_of_live_owner_is_untouched(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """Live owners keep their pending chats: only terminated owners have no
        consumer, so the sweep never touches idling/running queues."""
        from services.delivery_watchdog.daemon import dead_letter_stale_pending_chats

        aid = _make_idling_agent(db_conn)
        row = _insert_pending_resurrect_row(db_conn, aid, age_s=2 * 86400, kind="chat")

        assert dead_letter_stale_pending_chats(pool, 86400.0) == 0
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (row,))
            assert cur.fetchone() == ("pending",)

    def test_old_non_chat_pending_row_is_untouched(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """Lifecycle kinds keep their own sweep; this one is chat-only."""
        from services.delivery_watchdog.daemon import dead_letter_stale_pending_chats

        aid = _make_terminated_agent(db_conn)
        row = _insert_pending_resurrect_row(db_conn, aid, age_s=2 * 86400, kind="terminate")

        assert dead_letter_stale_pending_chats(pool, 86400.0) == 0
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (row,))
            assert cur.fetchone() == ("pending",)


class TestSelectTerminatedOwnersWithPending:
    def test_force_fence_excludes_older_chat_but_accepts_newer_chat(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """The selector uses the monotonic explicit-kill fence in addition to
        wall-clock status time: old queued work stays dead, later work wakes."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
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
    ) -> None:
        """A user's explicit kill wins over mail already waiting when they
        killed the agent; that old row must not immediately undo the kill."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_idling_agent(db_conn)
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
    ) -> None:
        """A new chat sent after termination preserves the existing contract:
        delivery to a dead agent wakes it automatically."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
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
    ) -> None:
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
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
    ) -> None:
        """250 dead letters for one agent mean ONE resurrect, not 250."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
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
    ) -> None:
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        live = _make_idling_agent(db_conn)  # idling owner — not a resurrect case
        insert_inbound_message(db_conn, live, "hi", source="user", bus=event_bus, database=database)
        dead = _make_terminated_agent(db_conn)
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
    ) -> None:
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
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
    ) -> None:
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
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
    ) -> None:
        """Issue #2049: the ghost-alive state — a terminated owner whose only
        pending chats are past the stale threshold — is not a resurrect trigger."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
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
    ) -> None:
        """Inside the threshold the G4 retry window is unchanged: a recent
        post-termination chat still wakes its terminated owner."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
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
    ) -> None:
        """A chat already waiting when the SYSTEM reaped a crash-marked corpse
        is leftover work, not mail an operator's kill cancelled — the relaxed
        fence lets it trigger resurrection (task #3617, design #3610 section 6)."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_reaped_crash_agent(db_conn)
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
    ) -> None:
        """`reaper` alone — marker already cleared by a completed turn of the
        revived incarnation — is an ordinary system death: the fence holds."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
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
    ) -> None:
        """The crash marker alone never relaxes the fence: only the SYSTEM's
        own reap is not an operator decision (user/exit) and not a launch or
        integrity death (which keep their own semantics)."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
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
    ) -> None:
        """The relaxed fence does not bypass the automatic-recovery gates: an
        active wake suppression refuses, an expired one does not, and a
        tripped recovery circuit breaker refuses even without a window
        (task #3617; the streak is the durable gate)."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_reaped_crash_agent(db_conn)
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
    ) -> None:
        """The remaining conjuncts are untouched: past the dead-letter bound
        the row is no trigger, and a later explicit force fence still wins."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_reaped_crash_agent(db_conn)
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
    ) -> None:
        """A failed-restart target keeps its own hard fence: the relaunch
        observation must settle before any resurrection, reaped crash row or
        not."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_reaped_crash_agent(db_conn)
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
    ) -> None:
        """A system-family chat is a platform notification, never a resurrect
        trigger: plain 'system' and every 'system:<subtype>' variant must not
        select, while a real chat on the same owner still does (task #3687 —
        the watcher-reap notice that woke 6260 twice)."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
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
    ) -> None:
        """Machine wakeups (watcher: / shell: / schedule:) are deliberately NOT
        notices: a crash-reaped owner's watcher wake is a revival channel, so
        they must keep selecting (task #3687 boundary review)."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
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
    ) -> None:
        """The watchdog's hosted-turn recovery chat is the one system-source
        chat that must stay selected: it is this scan's durable retry for a
        wedged hosted turn, so the payload marker flips the verdict on the G4
        channel too. Only the exact JSON boolean `true` counts — a string
        "true" fails closed (task #3687 review, Ava #3242)."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
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


class TestSystemNoticeSourcePredicateParity:
    """`SYSTEM_NOTICE_SOURCE` (SQL, consumed by the selector) and
    `is_system_notice_source` (Python, consumed by the resurrect endpoint)
    gate the same decision from two languages; they must agree on every
    `(source, payload)` input — a one-sided edit would reopen the 6260 gap
    from the other side (task #3687 review note: pin the pair together). The
    payload dimension covers the hosted-turn-recovery carve-out and its
    fail-closed marker rule: only the exact JSON boolean `true` exempts; a
    missing key, JSON null, or any other value (even the string "true") stays
    a notice (review requirement, Ava #3242)."""

    # (label, payload, exempt-from-notice-verdict)
    _PAYLOAD_SAMPLES: tuple[tuple[str, dict[str, object] | None, bool], ...] = (
        ("payload-absent", None, False),
        ("key-absent", {"content_blocks": []}, False),
        ("json-null", {"hosted_turn_recovery": None}, False),
        ("boolean-false", {"hosted_turn_recovery": False}, False),
        ("string-false", {"hosted_turn_recovery": "false"}, False),
        ("number-1", {"hosted_turn_recovery": 1}, False),
        ("string-1", {"hosted_turn_recovery": "1"}, False),
        ("string-true", {"hosted_turn_recovery": "true"}, False),
        ("boolean-true", {"hosted_turn_recovery": True}, True),
    )

    def test_sql_fragment_and_python_twin_agree(self, db_conn: psycopg.Connection) -> None:
        from psycopg import sql
        from psycopg.types.json import Jsonb

        from base.agents.incarnation.lifecycle_acceptance import (
            SYSTEM_NOTICE_SOURCE,
            is_system_notice_source,
        )

        sources = [
            "system",
            "system:",
            "system:notice-reply",
            "system:warn-error-audit",
            "systemwarn",
            "system%d",
            "system_x",
            "System",
            " system",
            "system :x",
            "",
            "user",
            "agent:1",
            "ui:web",
            "watcher:3",
            "shell:0",
            "schedule:2",
        ]
        with db_conn.cursor() as cur:
            for source in sources:
                for label, payload, _exempt in self._PAYLOAD_SAMPLES:
                    cur.execute(
                        sql.SQL(
                            "SELECT {} FROM (SELECT %s::text AS source, %s::jsonb AS payload) AS m"
                        ).format(sql.SQL(SYSTEM_NOTICE_SOURCE)),
                        (source, Jsonb(payload) if payload is not None else None),
                    )
                    row = cur.fetchone()
                    assert row is not None, (source, label)
                    assert row[0] == is_system_notice_source(source, payload), (source, label)

    def test_only_the_exact_boolean_true_marker_exempts(self) -> None:
        from base.agents.incarnation.lifecycle_acceptance import is_system_notice_source

        for source in ("system", "system:notice-reply"):
            for label, payload, exempt in self._PAYLOAD_SAMPLES:
                expected = not exempt
                assert is_system_notice_source(source, payload) is expected, (source, label)
        # The marker only ever applies inside the system family.
        assert is_system_notice_source("watcher:3", {"hosted_turn_recovery": True}) is False


def _make_crash_marked_agent(db: psycopg.Connection) -> int:
    """An idling row with the corpse marker set — the corpse reaper's own
    predicate (`last_turn_fatal_at IS NOT NULL` on an idling row).
    spawn_agent leaves the marker NULL, so the scenario stamps it."""
    from tests.fixtures.units import spawn_agent

    aid = spawn_agent(spawner="user")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'idling', last_turn_fatal_at = now() WHERE id = %s",
            (aid,),
        )
    db.commit()
    return aid
