"""`services.wake.delivery_watchdog.daemon` — stale-pending-inbound selection + alerting.

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
from base.config.service_read import ConfigAuthority
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from base.events.live.redis_listener import RedisInboundListener
from base.lm.catalog import ModelCatalog
from services.wake.delivery_watchdog.daemon import (
    dispatch_wakes,
    scan_once,
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


def _make_idling_agent(
    db: psycopg.Connection, *, model_catalog: ModelCatalog, config_authority: ConfigAuthority
) -> int:
    """spawn_agent creates the agents_meta row (create_agent does not — that
    is the spawn path's job); the alert filter reads owner status, so tests
    spawn then park the agent 'idling' (same pattern as the heartbeat daemon
    tests)."""
    from tests.fixtures.units import spawn_agent

    aid = spawn_agent(spawner="user", catalog=model_catalog, authority=config_authority)
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'idling' WHERE id = %s", (aid,))
    db.commit()
    return aid


def _make_running_agent(
    db: psycopg.Connection, *, model_catalog: ModelCatalog, config_authority: ConfigAuthority
) -> int:
    from tests.fixtures.units import spawn_agent

    aid = spawn_agent(spawner="user", catalog=model_catalog, authority=config_authority)
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
        iid = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status = 'claimed' WHERE id = %s", (iid,))
        db_conn.commit()
        assert select_stale_pending(pool, _THRESHOLD_S) == []

    def test_empty_when_nothing_stale(
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
        _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S - 1)
        assert select_stale_pending(pool, _THRESHOLD_S) == []

    def test_running_owner_queues_are_not_stalls(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """A chat inbound queued behind a long in-flight turn (owner
        status='running') is normal, not a delivery stall — the turn-end SELECT
        picks it up. Only waiting/terminal owners signal a real stall."""
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (aid,))
        db_conn.commit()
        assert select_stale_pending(pool, _THRESHOLD_S) == []


class TestScanOnce:
    def test_alerts_each_stale_row_once_while_pending(
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
        iid = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5)

        newly, alerted = scan_once(pool, _THRESHOLD_S, set())
        assert newly == 1
        assert alerted == {iid}

        # Second scan: still stale, already alerted -> no new alerts, no spam.
        newly, alerted = scan_once(pool, _THRESHOLD_S, alerted)
        assert newly == 0
        assert alerted == {iid}

    def test_row_that_leaves_pending_forgets_alert(
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
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """The alert emits through the unified emitter: the canonical
        `events` row (telemetry/delivery_stalled). The legacy agent_events
        mirror is gone (tracker #898 term-alignment)."""
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
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
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """dispatch_wakes re-publishes one wake (payload = inbound id) per
        stale pending row of an idling owner — the lost-wake recovery."""
        import base.db

        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
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
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """A failing publish is logged, not raised — the alert path and the
        claim loop's 30s recheck remain as backstops."""
        import base.db

        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
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
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """End-to-end: dispatch_wakes publishes on the agent's Redis channel,
        so a listener subscribed to it wakes immediately — the lost-wake window
        collapses from 30s to ~1 tick."""
        from base.events.live.redis_listener import RedisInboundListener

        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
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


def _delivery_poisoned_events(agent_id: int) -> list[dict[str, object]]:
    """The `delivery_poisoned` telemetry lines for `agent_id` in today's JSONL mirror."""
    import json
    from datetime import UTC, datetime

    from base.paths import logs_dir

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
            and event.get("agent_id") == agent_id
            and event.get("category") == "telemetry"
        ):
            events.append(event)
    return events


def _wait_for_poisoned_events(agent_id: int) -> list[dict[str, object]]:
    """Poll briefly: the emitter drains asynchronously, so the line may land late."""
    events: list[dict[str, object]] = []
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        events = _delivery_poisoned_events(agent_id)
        if events:
            break
        time.sleep(0.05)
    return events


# ── Terminated-owner resurrect retry (Task #689 G4) ───────────────────────────


def _make_terminated_agent(
    db: psycopg.Connection, *, model_catalog: ModelCatalog, config_authority: ConfigAuthority
) -> int:
    from tests.fixtures.units import spawn_agent

    aid = spawn_agent(spawner="user", catalog=model_catalog, authority=config_authority)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'terminated', termination_source = 'exit' "
            "WHERE id = %s",
            (aid,),
        )
    db.commit()
    return aid


def _make_reaped_crash_agent(
    db: psycopg.Connection, *, model_catalog: ModelCatalog, config_authority: ConfigAuthority
) -> int:
    """A `terminated` row the SYSTEM reaped after a crash: reaper source plus
    the retained crash marker (task #3617's relaxed-trigger population)."""
    aid = _make_terminated_agent(db, model_catalog=model_catalog, config_authority=config_authority)
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


def _make_crash_marked_agent(
    db: psycopg.Connection, *, model_catalog: ModelCatalog, config_authority: ConfigAuthority
) -> int:
    """An idling row with the corpse marker set — the corpse reaper's own
    predicate (`last_turn_fatal_at IS NOT NULL` on an idling row).
    spawn_agent leaves the marker NULL, so the scenario stamps it."""
    from tests.fixtures.units import spawn_agent

    aid = spawn_agent(spawner="user", catalog=model_catalog, authority=config_authority)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'idling', last_turn_fatal_at = now() WHERE id = %s",
            (aid,),
        )
    db.commit()
    return aid
