"""The wake-dispatch host gate — route D of task #4872.

`services/wake/delivery_watchdog.dispatch_guard` re-dispatches and poisons a
pending inbound only while the owner's `machine_probe` verdict is fresh:
the machine not graded offline (fewer than two consecutive probe failures)
and the agent host alive — the host check is excused inside the one-failure
grace window, where a failed probe nulls the verdict. A missing or stale
row, a machine graded offline, or an absent/dead host verdict outside that
window freezes the row — no publish, no dispatch-count advance, no poison —
and redelivery resumes on the next round once the verdict is fresh again.
"""

from __future__ import annotations

import time

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base import telemetry
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.wake.delivery_watchdog.daemon import (
    dispatch_wakes,
    select_pending_for_dispatch,
)

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


def _make_idling_agent(
    db: psycopg.Connection, *, model_catalog: ModelCatalog, config_authority: ConfigAuthority
) -> int:
    """spawn_agent creates the agents_meta row (create_agent does not — that
    is the spawn path's job); the dispatch filter reads owner status, so tests
    spawn then park the agent 'idling'."""
    from tests.fixtures.units import spawn_agent

    aid = spawn_agent(spawner="user", catalog=model_catalog, authority=config_authority)
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'idling' WHERE id = %s", (aid,))
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
    agent_host: bool | None = True,
    consecutive_failures: int = 0,
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
            "VALUES (%s, %s, %s, %s, now() - make_interval(secs => %s)) "
            "ON CONFLICT (machine_name) DO UPDATE SET online = EXCLUDED.online, "
            "agent_host_online = EXCLUDED.agent_host_online, "
            "consecutive_failures = EXCLUDED.consecutive_failures, "
            "last_probe_at = EXCLUDED.last_probe_at",
            (name, online, agent_host, consecutive_failures, age_s),
        )
    db.commit()


class TestSelectPendingForDispatch:
    def test_returns_pending_of_idling_owners_older_than_threshold(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """All kinds count (a lost wake strands terminate/restart too), any
        kind of stale pending of an idling owner is dispatched."""
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
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
            _HOST_STALENESS_S,
        )
        assert {r[0] for r in rows} == {old_chat, term_id}
        assert all(r[1] == aid for r in rows)

    def test_fresh_rows_and_non_idling_owners_not_dispatched(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """Fresh rows (still within the dispatch threshold) and owners not in
        'idling' (running = mid-turn queue, terminated = its own controller)
        are left alone."""
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
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
                _HOST_STALENESS_S,
            )
            == []
        )

    def test_wake_suppression_excludes_until_expiry(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
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
                _HOST_STALENESS_S,
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
            _HOST_STALENESS_S,
        ) == [(iid, aid)]

    # Route D (task #4872): a fresh healthy host verdict gates dispatch and
    # poisoning alike; anything else freezes the row and nothing is burned.

    @staticmethod
    def _select(pool: ConnectionPool) -> list[tuple[int, int]]:
        return select_pending_for_dispatch(
            pool,
            _DISPATCH_THRESHOLD_S,
            _MAX_DISPATCH_COUNT,
            _DISPATCH_BACKOFF_STEPS_S,
            _HOST_STALENESS_S,
        )

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

    def test_machine_offline_freezes_then_recovery_resumes(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        publishes: list[tuple[int, str]] = []

        def record_publish(_db: object, _bus: object, agent_id: int, payload: str) -> bool:
            publishes.append((agent_id, payload))
            return True

        monkeypatch.setattr("base.db.publish_inbound_wake", record_publish)
        _set_host_verdict(db_conn, online=False, agent_host=None, consecutive_failures=2)
        assert self._dispatch(pool) == 0
        assert publishes == []
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT dispatch_count, last_dispatch_at, poisoned_at "
                "FROM inbound_messages WHERE id = %s",
                (iid,),
            )
            assert cur.fetchone() == (0, None, None)

        _set_host_verdict(db_conn)  # host recovers: fresh healthy verdict
        assert self._dispatch(pool) == 1
        assert publishes == [(aid, str(iid))]

    def test_single_failed_probe_still_dispatches(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """Real writer shape for a lone failed probe — online=false,
        consecutive_failures=1, agent_host_online NULL (a failed probe nulls
        the host verdict; services/wake/heartbeat/liveness.py) — stays inside the
        one-failure grace window, so the wake is dispatched."""
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        publishes: list[tuple[int, str]] = []

        def record_publish(_db: object, _bus: object, agent_id: int, payload: str) -> bool:
            publishes.append((agent_id, payload))
            return True

        monkeypatch.setattr("base.db.publish_inbound_wake", record_publish)
        _set_host_verdict(db_conn, online=False, agent_host=None, consecutive_failures=1)
        assert self._dispatch(pool) == 1
        assert publishes == [(aid, str(iid))]

    def test_missing_host_verdict_outside_grace_freezes_dispatch(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """A NULL host verdict with no failed probe yet (cf=0) sits outside
        the grace window: an absent verdict freezes."""
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        _set_host_verdict(db_conn, agent_host=None)
        assert self._select(pool) == []

    def test_hostless_machine_freezes_dispatch(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        _set_host_verdict(db_conn, agent_host=False)
        assert self._select(pool) == []

    def test_stale_and_missing_verdicts_freeze_dispatch(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        from base.cluster.machine import machine_name

        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        _set_host_verdict(db_conn, age_s=_HOST_STALENESS_S + 1)
        assert self._select(pool) == []
        with db_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM machine_probe WHERE machine_name = %s",
                (machine_name(),),
            )
        db_conn.commit()
        assert self._select(pool) == []

    def test_poison_suppressed_while_host_down_then_fires_on_recovery(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        import json
        from datetime import UTC, datetime

        from base.paths import logs_dir

        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages SET dispatch_count = %s WHERE id = %s",
                (_MAX_DISPATCH_COUNT, iid),
            )
        db_conn.commit()

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

        _set_host_verdict(db_conn, agent_host=False)
        assert self._dispatch(pool) == 0
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT poisoned_at IS NULL, status FROM inbound_messages WHERE id = %s",
                (iid,),
            )
            assert cur.fetchone() == (True, "pending")
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            telemetry.flush()
            time.sleep(0.05)
        assert poisoned_events() == []

        _set_host_verdict(db_conn)
        # At the cap there is nothing left to dispatch; the poison pass fires now.
        assert self._dispatch(pool) == 0
        events: list[dict[str, object]] = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            events = poisoned_events()
            if events:
                break
            time.sleep(0.05)
        assert len(events) == 1
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT poisoned_at IS NOT NULL FROM inbound_messages WHERE id = %s",
                (iid,),
            )
            assert cur.fetchone() == (True,)


@pytest.fixture(autouse=True)
def _healthy_host_verdict(db_conn: psycopg.Connection) -> None:
    """Normal dispatch condition: a fresh reachable machine with a live host.

    Host-gate tests override the verdict inside the test body."""
    _set_host_verdict(db_conn)
