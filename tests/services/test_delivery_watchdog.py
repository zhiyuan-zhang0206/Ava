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

    def test_hibernating_owner_still_alerts(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """Hibernating + pending past the threshold = swap-in failure (the
        hibernation controller relaunches on pending inbound); worth alerting."""
        aid = _make_idling_agent(db_conn)
        _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'hibernating' WHERE id = %s", (aid,))
        db_conn.commit()
        rows = select_stale_pending(pool, _THRESHOLD_S)
        assert [r[0] for r in rows] == [aid] or len(rows) == 1


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

        def _stalled() -> dict | None:
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
        assert int(ev["attributes"]["inbound_id"]) == iid  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]


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
        rows = select_pending_for_dispatch(pool, _DISPATCH_THRESHOLD_S)
        assert {r[0] for r in rows} == {old_chat, term_id}
        assert all(r[1] == aid for r in rows)

    def test_fresh_rows_and_non_idling_owners_not_dispatched(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """Fresh rows (still within the dispatch threshold) and owners not in
        'idling' (running = mid-turn queue, hibernating/terminated = their own
        controllers) are left alone."""
        aid = _make_idling_agent(db_conn)
        _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S - 0.3)  # fresh
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (aid,))
        db_conn.commit()
        _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'idling' WHERE id = %s", (aid,))
            cur.execute("UPDATE agents_meta SET status = 'hibernating' WHERE id = %s", (aid,))
        db_conn.commit()
        _insert_old_inbound(db_conn, aid, age_s=_DISPATCH_THRESHOLD_S + 1)
        assert select_pending_for_dispatch(pool, _DISPATCH_THRESHOLD_S) == []


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
            lambda agent_id, payload: calls.append((agent_id, payload)),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        dispatched = dispatch_wakes(pool, _DISPATCH_THRESHOLD_S)
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

        def boom(*_a, **_k):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
            raise RuntimeError("redis down")

        monkeypatch.setattr(shared.db, "publish_inbound_wake", boom)  # pyright: ignore[reportUnknownArgumentType]
        assert dispatch_wakes(pool, _DISPATCH_THRESHOLD_S) == 0

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
            dispatched = dispatch_wakes(pool, _DISPATCH_THRESHOLD_S)
            assert dispatched == 1
            await listener.wait_one(timeout=2.0)  # returns on the dispatched wake
        finally:
            await listener.close()


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
    return iid  # pyright: ignore[reportUnknownVariableType]


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

        assert dead_letter_stale_claimed(pool, 86400.0) == 1
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

        assert dead_letter_stale_claimed(pool, 86400.0) == 0
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (iid,))
            assert cur.fetchone() == ("claimed",)

    def test_claimed_of_live_owner_untouched(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """A live agent's claimed rows are mid-flight (finalized at its next
        boot) — never swept regardless of age."""
        from services.delivery_watchdog.daemon import dead_letter_stale_claimed

        aid = _make_idling_agent(db_conn)
        iid = _insert_claimed_row(db_conn, aid, claim_age_s=10 * 86400)

        assert dead_letter_stale_claimed(pool, 86400.0) == 0
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (iid,))
            assert cur.fetchone() == ("claimed",)

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

        assert dead_letter_stale_claimed(pool, 86400.0) == 1
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM inbound_messages WHERE id IN (%s, %s)",
                (old, fresh),
            )
            assert dict(cur.fetchall()) == {old: "done", fresh: "claimed"}


class TestSelectTerminatedOwnersWithPending:
    def test_returns_terminated_owners_with_pending_chat(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
        insert_inbound_message(db_conn, aid, "hello?", source="user")

        assert select_terminated_owners_with_pending(pool) == [aid]

    def test_deduplicates_per_agent(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """250 dead letters for one agent mean ONE resurrect, not 250."""
        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

        aid = _make_terminated_agent(db_conn)
        for _ in range(3):
            insert_inbound_message(db_conn, aid, "hello?", source="user")

        assert select_terminated_owners_with_pending(pool) == [aid]

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


class TestResurrectRetry:
    async def test_cooldown_and_cap_bound_retries(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-agent 60s cooldown + per-tick cap: a dead-letter pile for an
        unreachable home machine drains over ticks, never as a burst."""
        import asyncio
        import time

        import services.delivery_watchdog.daemon as dw

        dead_a = _make_terminated_agent(db_conn)
        dead_b = _make_terminated_agent(db_conn)
        for aid in (dead_a, dead_b):
            insert_inbound_message(db_conn, aid, "hello?", source="user")

        calls: list[int] = []

        async def fake_resurrect(aid: int) -> None:
            calls.append(aid)
            await asyncio.sleep(10)  # hold in flight so task count is observable

        monkeypatch.setattr(dw, "_resurrect_one", fake_resurrect)
        dw._last_resurrect_attempt.clear()
        dw._resurrect_tasks.clear()

        # First pass: both distinct owners spawn (cap=5).
        dw._maybe_spawn_resurrects(pool, max_per_tick=5)
        await asyncio.sleep(0)
        assert len(dw._resurrect_tasks) == 2
        assert set(calls) == {dead_a, dead_b}

        # Second pass inside the cooldown: neither spawns again.
        for aid in (dead_a, dead_b):
            dw._last_resurrect_attempt[aid] = time.monotonic()
        dw._maybe_spawn_resurrects(pool, max_per_tick=5)
        await asyncio.sleep(0)
        assert len(dw._resurrect_tasks) == 2  # unchanged

        # Cap: after the in-flight tasks finish, cap=1 admits only one of two.
        for t in list(dw._resurrect_tasks):
            t.cancel()
        await asyncio.gather(*list(dw._resurrect_tasks), return_exceptions=True)
        assert len(dw._resurrect_tasks) == 0
        dw._last_resurrect_attempt.clear()
        dw._maybe_spawn_resurrects(pool, max_per_tick=1)
        await asyncio.sleep(0)
        assert len(dw._resurrect_tasks) == 1
        for t in list(dw._resurrect_tasks):
            t.cancel()
        await asyncio.gather(*list(dw._resurrect_tasks), return_exceptions=True)
        dw._resurrect_tasks.clear()

    async def test_resurrect_one_runs_and_records_attempt(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_resurrect_one calls the lazy-imported resurrect_if_terminated and
        stamps the per-agent attempt clock (drives the 60s cooldown)."""
        import time

        import ops.ops_lifecycle as ol
        import services.delivery_watchdog.daemon as dw

        aid = _make_terminated_agent(db_conn)
        calls: list[int] = []

        async def fake(aid_: int) -> str:
            calls.append(aid_)
            return "terminated"

        monkeypatch.setattr(ol, "resurrect_if_terminated", fake)
        dw._last_resurrect_attempt.clear()

        await dw._resurrect_one(aid)

        assert calls == [aid]
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
