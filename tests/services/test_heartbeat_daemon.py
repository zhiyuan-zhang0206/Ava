"""`services.heartbeat.daemon` — idle-agent check-in selection + the pause window.

`_select_idle_agents_needing_heartbeat` is the daemon's core predicate. The idle
clock is `last_active_at` (the last completed LLM turn — real work), NOT
`status_changed_at` (bumped by every status flip, including ops lifecycle churn).
`TestIdleClockCountsRealActivityOnly` pins that semantic: an ops restart resets
status_changed_at without a real turn and must not reset the idle clock. The
pause window is a floor on the next check-in time. A real turn during the window
starts the normal idle clock, so after the window expires the agent still waits
`last_active_at + idle_threshold` (plus its deterministic jitter offset).
"""

from __future__ import annotations

import asyncio
import time

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from services.heartbeat.daemon import (
    _select_idle_agents_needing_heartbeat,
    _send_heartbeat_checkin,
)
from shared import telemetry
from shared.config import settings
from shared.redis_listener import RedisInboundListener
from tests.conftest import spawn_agent

# Explicit threshold so the assertions do not ride on the configured default.
_THRESHOLD_S = 300.0


@pytest.fixture
def pool():
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


def _make_idle(
    db: psycopg.Connection,
    *,
    status_changed_s_ago: float,
    last_active_s_ago: float | None = None,
    paused_until_s_ahead: float | None = None,
    status: str = "idling",
) -> int:
    """Spawn an agent and park it. `status_changed_s_ago` backdates
    status_changed_at via a timestamp-only UPDATE (the BEFORE-UPDATE-OF-status
    trigger fires on the status flip, not on this). `last_active_s_ago` backdates
    last_active_at — the real-activity clock the daemon actually keys off;
    defaults to `status_changed_s_ago` so a plain idle agent has the two aligned
    (the common case: it entered idling right after its last turn). Pass the two
    independently to model an ops restart, which bumps status_changed_at (fresh)
    without a real turn (last_active_at stays old). `paused_until_s_ahead` sets
    heartbeat_paused_until relative to now() — negative = an already-expired pause
    window. Returns the agent id."""
    if last_active_s_ago is None:
        last_active_s_ago = status_changed_s_ago
    aid = spawn_agent(spawner="user")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = %s, "
            "lease_expires_at = now() + make_interval(secs => 600) WHERE id = %s",
            (status, aid),
        )
        cur.execute(
            "UPDATE agents_meta SET status_changed_at = now() - make_interval(secs => %s), "
            "       last_active_at = now() - make_interval(secs => %s) "
            "WHERE id = %s",
            (status_changed_s_ago, last_active_s_ago, aid),
        )
        if paused_until_s_ahead is not None:
            cur.execute(
                "UPDATE agents_meta SET heartbeat_paused_until = now() + make_interval(secs => %s) "
                "WHERE id = %s",
                (paused_until_s_ahead, aid),
            )
    db.commit()
    return aid


def _selected(pool: ConnectionPool) -> dict[int, float]:
    """agent_id -> idle_minutes for every agent the daemon would check in on."""
    return dict(_select_idle_agents_needing_heartbeat(pool, _THRESHOLD_S))


class TestSelectIdleAgents:
    def test_idle_past_threshold_unpaused_is_selected(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        aid = _make_idle(db_conn, status_changed_s_ago=400)
        selected = _selected(pool)
        assert aid in selected
        assert float(selected[aid]) == pytest.approx(400 / 60.0, abs=0.5)  # pyright: ignore[reportUnknownMemberType]

    def test_idling_without_a_live_lease_is_not_selected(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """R1 (Task #1021): an idling row whose lease expired (or was never
        granted) is a zombie the reaper is collecting — nudging it would only
        keep a corpse busy. The daemon must not select it."""
        from datetime import UTC, datetime, timedelta

        aid = _make_idle(db_conn, status_changed_s_ago=400)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET lease_expires_at = %s WHERE id = %s",
                (datetime.now(UTC) - timedelta(seconds=1), aid),
            )
        db_conn.commit()
        assert aid not in _selected(pool)

    def test_preclaim_idling_row_is_not_selected(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """A just-spawned idling row has no process or lease yet. It is not an
        idle process to nudge; launch confirmation and the dead-birth reaper own
        that interval."""
        aid = _make_idle(db_conn, status_changed_s_ago=400)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET pid = NULL, started_at = NULL, lease_expires_at = NULL "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert aid not in _selected(pool)

    def test_hibernating_is_selected_without_any_lease(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """A hibernating agent has no process and no lease by design (swapped
        out) — the nudge is exactly how it wakes, so it must stay selectable
        even with a NULL lease (R1, Task #1021: reaper-exempt)."""
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'hibernating', lease_expires_at = NULL, "
                "last_active_at = now() - make_interval(secs => 400) WHERE id = %s",
                (_make_idle(db_conn, status_changed_s_ago=400),),
            )
        db_conn.commit()
        # sanity: the parked row has no lease
        with db_conn.cursor() as cur:
            cur.execute("SELECT lease_expires_at FROM agents_meta WHERE status = 'hibernating'")
            assert cur.fetchone() == (None,)
        assert _selected(pool) != {}

    def test_idle_under_threshold_unpaused_excluded(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """Scenario 1: a recent wake (status_changed_at fresh) leaves the agent
        under the idle threshold, so it is not yet due."""
        aid = _make_idle(db_conn, status_changed_s_ago=120)
        assert aid not in _selected(pool)

    def test_running_agent_excluded(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """Only idling agents get a check-in; a running one is active by definition."""
        aid = _make_idle(db_conn, status_changed_s_ago=400, status="running")
        assert aid not in _selected(pool)

    def test_hibernating_agent_is_selected(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """Hibernating agents get check-ins too — the heartbeat is the liveness
        signal, so a swapped-out agent must still be woken (its check-in inbound is
        then picked up by the hibernation controller's swap-in). last_active_at
        survives the swap-out untouched, so its due-time is computed exactly as
        when idling."""
        aid = _make_idle(db_conn, status_changed_s_ago=400, status="hibernating")
        assert aid in _selected(pool)

    def test_hibernating_paused_agent_not_selected_until_pause_expires(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """A hibernating agent that paused its heartbeat (H > heartbeat threshold
        makes the dominant hibernating case the paused one) gets NO check-in while the
        pause window is in the future — the pause is honoured even swapped out, so
        the agent stays hibernating for the whole pause."""
        aid = _make_idle(
            db_conn, status_changed_s_ago=600, status="hibernating", paused_until_s_ahead=1800
        )
        assert aid not in _selected(pool)

    def test_hibernating_agent_woken_when_pause_expires(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """Once the pause window expires, the heartbeat checks in on the hibernating
        agent (its check-in inbound then drives the controller swap-in). Covers the
        pause-expired + hibernating combo the swap-in poll relies on."""
        aid = _make_idle(
            db_conn, status_changed_s_ago=600, status="hibernating", paused_until_s_ahead=-60
        )
        assert aid in _selected(pool)

    def test_pending_inbound_excluded(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """An agent about to wake on a real message does not also need a check-in."""
        aid = _make_idle(db_conn, status_changed_s_ago=400)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (%s, 'hi', 'chat', 'user')",
                (aid,),
            )
        db_conn.commit()
        assert aid not in _selected(pool)

    def test_active_pause_suppresses_even_when_idle_past_threshold(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """Scenario 2: while the pause window is in the future, the agent is
        skipped even though it has been idle far longer than the threshold."""
        aid = _make_idle(db_conn, status_changed_s_ago=600, paused_until_s_ahead=1800)
        assert aid not in _selected(pool)

    def test_real_turn_during_pause_delays_checkin_past_window(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """R-6: the pause window is a floor, not an absolute check-in time. A
        real turn during the window starts the normal idle clock, so after expiry
        the agent must wait the idle threshold instead of taking a wasted wake."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=10,
            last_active_s_ago=10,
            paused_until_s_ahead=-1,
        )
        assert aid not in _selected(pool)

    def test_open_pause_window_still_suppresses_checkin_after_real_turn(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """The R-6 pause floor wins while it is open, then the real turn's
        idle clock wins after expiry; both edges are one unified due-time rule."""
        open_window = _make_idle(
            db_conn,
            status_changed_s_ago=10,
            last_active_s_ago=10,
            paused_until_s_ahead=60,
        )
        expired_window = _make_idle(
            db_conn,
            status_changed_s_ago=10,
            last_active_s_ago=10,
            paused_until_s_ahead=-1,
        )
        selected = _selected(pool)
        assert open_window not in selected
        assert expired_window not in selected

    def test_pause_expired_before_last_wake_uses_normal_clock(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """Once a wake post-dates the expired pause window (status_changed_at >
        heartbeat_paused_until), the normal wake-resettable clock resumes: an
        agent idle only 120s is under the threshold and not yet due, NOT stuck
        firing forever on the stale past window."""
        aid = _make_idle(db_conn, status_changed_s_ago=120, paused_until_s_ahead=-300)
        assert aid not in _selected(pool)

    def test_pause_expired_after_long_idle_is_due(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """No intervening wake: the agent has been idle 600s and its pause
        window expired 60s ago. It is overdue under both regimes and selected."""
        aid = _make_idle(db_conn, status_changed_s_ago=600, paused_until_s_ahead=-60)
        assert aid in _selected(pool)


class TestIdleClockCountsRealActivityOnly:
    """The idle clock is last_active_at (real work), not status_changed_at (bumped
    by every status flip). These pin the semantic fix: an ops restart resets
    status_changed_at without a real turn and must NOT reset the idle clock."""

    def test_ops_restart_does_not_reset_idle_clock(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """An agent idle 400s (past threshold) then hit by an ops restart:
        status_changed_at is fresh (5s ago, the re-idle after respawn) but
        last_active_at is still 400s ago (the ops cycle ran no LLM turn). It stays
        due — the rollout did not zero its idle timer."""
        aid = _make_idle(db_conn, status_changed_s_ago=5, last_active_s_ago=400)
        assert aid in _selected(pool)

    def test_ops_restart_does_not_make_long_idle_agent_look_fresh(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """Contrast with the pre-fix behavior: keyed off status_changed_at the same
        agent (fresh status_changed_at) would read as only 5s idle and be excluded.
        Keyed off last_active_at it is correctly overdue. The idle_minutes reported
        reflects the real 400s, not the 5s since the ops re-idle."""
        aid = _make_idle(db_conn, status_changed_s_ago=5, last_active_s_ago=400)
        selected = _selected(pool)
        assert aid in selected
        assert float(selected[aid]) == pytest.approx(400 / 60.0, abs=0.5)  # pyright: ignore[reportUnknownMemberType]

    def test_recent_real_turn_resets_idle_clock(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """The mirror case: an agent whose status_changed_at is old (600s) but that
        just completed a real turn 30s ago (last_active_at fresh) is NOT due — real
        activity, unlike ops churn, does reset the idle clock."""
        aid = _make_idle(db_conn, status_changed_s_ago=600, last_active_s_ago=30)
        assert aid not in _selected(pool)


class TestWakeupStormFlattening:
    """Density-hardening controls: per-agent jitter de-phases the unpaused
    due-time, and `limit` caps the per-step batch (the global wake-rate ceiling),
    oldest-idle first."""

    def _backdate(self, db: psycopg.Connection, aid: int, secs: float) -> None:
        # The daemon keys the idle clock off last_active_at; backdate it (and
        # status_changed_at alongside, so the row reads like a genuinely long-idle
        # agent).
        with db.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status_changed_at = now() - make_interval(secs => %s), "
                "       last_active_at = now() - make_interval(secs => %s) "
                "WHERE id = %s",
                (secs, secs, aid),
            )
        db.commit()

    def test_jitter_offsets_due_time_by_id_mod_span(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """With jitter, the unpaused due-time is `threshold + (id mod span)`. An
        agent idle just short of its own jittered due-time is excluded; the same
        agent idle just past it is selected. Keyed on the real id so the offset is
        exact, not probabilistic."""
        span, threshold = 100.0, 300.0
        aid = _make_idle(db_conn, status_changed_s_ago=1)
        offset = aid % int(span)

        self._backdate(db_conn, aid, threshold + offset - 5)
        got = dict(_select_idle_agents_needing_heartbeat(pool, threshold, jitter_span_s=span))
        assert aid not in got, "idle just short of the jittered due-time must be excluded"

        self._backdate(db_conn, aid, threshold + offset + 5)
        got = dict(_select_idle_agents_needing_heartbeat(pool, threshold, jitter_span_s=span))
        assert aid in got, "idle just past the jittered due-time must be selected"

    def test_zero_jitter_matches_plain_threshold(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """jitter_span_s=0 (the default) collapses the offset to 0 — no
        divide-by-zero, identical to the un-jittered predicate."""
        aid = _make_idle(db_conn, status_changed_s_ago=350)
        got = dict(_select_idle_agents_needing_heartbeat(pool, 300.0, jitter_span_s=0.0))
        assert aid in got

    def test_limit_caps_batch_oldest_idle_first(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """`limit` bounds the batch; ordering is oldest-idle first so the most
        overdue agents drain ahead of fresher ones."""
        a_old = _make_idle(db_conn, status_changed_s_ago=900)
        a_mid = _make_idle(db_conn, status_changed_s_ago=600)
        a_new = _make_idle(db_conn, status_changed_s_ago=400)

        order = [r[0] for r in _select_idle_agents_needing_heartbeat(pool, _THRESHOLD_S)]
        assert order.index(a_old) < order.index(a_mid) < order.index(a_new)

        capped = _select_idle_agents_needing_heartbeat(pool, _THRESHOLD_S, limit=2)
        assert len(capped) == 2
        assert a_new not in [r[0] for r in capped]


def _mirror_nudged(agent_id: int) -> tuple[str, str, int] | None:
    """The latest heartbeat_nudged mirror line for `agent_id` — (event_name,
    level, idle_minutes), or None while the drain thread has not landed it."""
    import json
    from datetime import UTC, datetime

    from shared.paths import logs_dir

    day = datetime.now(UTC).strftime("%Y%m%d")
    path = logs_dir() / f"events-{day}.jsonl"
    if not path.exists():
        return None
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("event_name") == "heartbeat_nudged" and obj.get("agent_id") == agent_id:
            return obj["event_name"], obj["level"], int(obj["attributes"]["idle_minutes"])
    return None


class TestSendHeartbeatCheckin:
    def test_inserts_heartbeat_inbound_and_event(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        aid = spawn_agent(spawner="user")
        _send_heartbeat_checkin(pool, aid, 7.0)
        # The emitter drains asynchronously (0.5s cadence) — flush() can
        # race the drain thread for the queue, so poll briefly for the line.
        # The PG events copy is a read-only archive since the LGTM cutover
        # (task #1197 close-C): the durable local copy is the JSONL mirror.
        ev = None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            telemetry.flush()
            ev = _mirror_nudged(aid)
            if ev is not None:
                break
            time.sleep(0.05)
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT content, kind, source FROM inbound_messages WHERE agent_id = %s",
                (aid,),
            )
            inbound = cur.fetchone()
        assert inbound is not None
        content, kind, source = inbound
        assert kind == "heartbeat"
        assert source == "system"
        assert (
            content == "Heartbeat. Find something to do, or pause your heartbeat for some time."
        )  # idle-minutes detail lives in the event row, not the content (0064)
        assert ev is not None
        assert ev[0] == "heartbeat_nudged"
        assert ev[1] == "info"
        assert int(ev[2]) == 7

    async def test_publishes_redis_wake_to_target(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """The check-in publishes a Redis wake to the target agent's channel so an
        idle agent runs the heartbeat turn now instead of at its next SELECT
        recheck — park a per-agent listener, fire the check-in, assert it wakes."""
        aid = spawn_agent(spawner="user")
        listener = RedisInboundListener(settings.data_plane.redis_url, aid)
        try:
            wait_task = asyncio.create_task(listener.wait_one(timeout=10.0))
            await asyncio.sleep(0.2)  # let the subscribe take effect before the publish
            t0 = time.monotonic()
            await asyncio.to_thread(_send_heartbeat_checkin, pool, aid, 7.0)
            await asyncio.wait_for(wait_task, timeout=5.0)
            assert time.monotonic() - t0 < 5.0, (
                "heartbeat check-in did not wake the parked listener"
            )
        finally:
            await listener.close()
