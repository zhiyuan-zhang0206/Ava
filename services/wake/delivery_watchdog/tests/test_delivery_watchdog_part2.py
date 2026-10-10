"""`services.wake.delivery_watchdog.daemon` — stale-pending-inbound selection + alerting.

`select_stale_pending` is the daemon's core predicate: chat inbounds still
`pending` past the threshold. `scan_once` adds the once-per-row-while-stuck
alert semantics (a row that flips pending -> claimed -> pending re-alerts).
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database, insert_inbound_message
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.wake.delivery_watchdog.daemon import (
    gc_alerted,
    persist_alerted,
    prune_alerted,
    scan_once,
    select_alerted_ids,
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


def _insert_old_inbound(
    db: psycopg.Connection, agent_id: int, *, age_s: float, database_gate: ProcessDbGate
) -> int:
    """Insert a chat inbound backdated `age_s` (timestamp-only UPDATE — the
    inbound table has no triggers on created_at). Returns the inbound id."""
    iid = insert_inbound_message(
        db,
        agent_id,
        "stale",
        source="user",
        bus=EventBus.from_settings(),
        database=Database.from_settings(gate=database_gate),
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


class TestAlertDedupPersistence:
    """Task #945: the once-per-row alert set survives daemon restarts via the
    `delivery_watchdog_alerted` table — a restart must not re-report every
    still-stalled inbound, and a row that leaves pending must still be
    forgotten (so the pending -> claimed -> pending flip re-alerts)."""

    def test_persist_then_reload_seeds_alerted_set(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5, database_gate=database_gate)

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
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        # FK -> inbound_messages: only real inbound ids can be persisted.
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid_a = _insert_old_inbound(
            db_conn, aid, age_s=_THRESHOLD_S + 5, database_gate=database_gate
        )
        iid_b = _insert_old_inbound(
            db_conn, aid, age_s=_THRESHOLD_S + 5, database_gate=database_gate
        )
        persist_alerted(pool, {iid_a, iid_b})
        assert select_alerted_ids(pool) == {iid_a, iid_b}
        # A second persist of the same ids is a no-op (ON CONFLICT DO NOTHING).
        persist_alerted(pool, {iid_a, iid_b})
        assert select_alerted_ids(pool) == {iid_a, iid_b}

    def test_prune_forgets_rows_that_left_pending(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5, database_gate=database_gate)
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

    def test_gc_removes_old_rows(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        aid = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5, database_gate=database_gate)
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


class TestStalledCrashMarkedRecovery:
    """Watchdog escalation for a stalled chat on a crash-marked idling corpse
    (task #3618; `services.wake.delivery_watchdog.stall_recovery`): the selector and
    the breaker/suppression exclusions. The request loop is covered in
    `test_stall_recovery.py`."""

    def test_selector_matches_only_marked_idling_owners(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        from services.wake.delivery_watchdog import stall_recovery as sr

        zombie = _make_crash_marked_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        zombie_inbound = _insert_old_inbound(
            db_conn, zombie, age_s=_THRESHOLD_S + 5, database_gate=database_gate
        )
        healthy = _make_idling_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        _insert_old_inbound(db_conn, healthy, age_s=_THRESHOLD_S + 5, database_gate=database_gate)
        running = _make_running_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET last_turn_fatal_at = now() WHERE id = %s", (running,)
            )
        db_conn.commit()
        _insert_old_inbound(db_conn, running, age_s=_THRESHOLD_S + 5, database_gate=database_gate)
        fresh = _make_crash_marked_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        _insert_old_inbound(db_conn, fresh, age_s=1.0, database_gate=database_gate)

        rows = sr.select_stalled_crash_marked(pool, _THRESHOLD_S)
        assert [(r[0], r[1]) for r in rows] == [(zombie_inbound, zombie)]

    def test_selector_excludes_halted_owners(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """A tripped recovery breaker (durable streak) or a live suppression
        window keeps the scan from starting a recovery — task #3617's halt."""
        from services.wake.delivery_watchdog import stall_recovery as sr

        tripped = _make_crash_marked_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        _insert_old_inbound(db_conn, tripped, age_s=_THRESHOLD_S + 5, database_gate=database_gate)
        suppressed = _make_crash_marked_agent(
            db_conn, model_catalog=model_catalog, config_authority=config_authority
        )
        _insert_old_inbound(
            db_conn, suppressed, age_s=_THRESHOLD_S + 5, database_gate=database_gate
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET permanent_reject_streak = 2 WHERE id = %s", (tripped,)
            )
            cur.execute(
                "UPDATE agents_meta SET wake_suppressed_until = now() + interval '1 hour', "
                "wake_suppress_reason = 'permanent_provider_reject' WHERE id = %s",
                (suppressed,),
            )
        db_conn.commit()
        assert sr.select_stalled_crash_marked(pool, _THRESHOLD_S) == []
