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
    _backoff_deadlines,
    _reconcile_checkin_outcomes,
    _select_idle_agents_needing_heartbeat,
    _send_heartbeat_checkin,
    _sweep_backoff_resets,
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

    def test_idling_without_a_lease_is_selected(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """An idle hosted agent has no turn lease and can still receive a check-in."""
        aid = _make_idle(db_conn, status_changed_s_ago=_THRESHOLD_S + 60)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET lease_expires_at = NULL, pid = NULL WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert aid in _selected(pool)

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

    def test_consumed_heartbeat_defers_the_next_checkin(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """A completed check-in must start a durable reminder interval.

        Regression for #5759: a permanent provider rejection leaves
        ``last_active_at`` unchanged. Once its heartbeat inbound is consumed,
        the pending-inbound guard no longer applies, so the daemon must still
        hold the agent until the configured heartbeat interval has elapsed.
        """
        aid = _make_idle(db_conn, status_changed_s_ago=400)
        _send_heartbeat_checkin(pool, aid, 7.0)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status = 'done' WHERE agent_id = %s", (aid,))
        db_conn.commit()

        assert aid not in _selected(pool)

    def test_reminder_uses_heartbeat_interval_not_dispatch_step(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """The 15-second dispatcher step must not become the reminder cadence."""
        aid = _make_idle(db_conn, status_changed_s_ago=400)
        _send_heartbeat_checkin(pool, aid, 7.0)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status = 'done' WHERE agent_id = %s", (aid,))
            cur.execute(
                "UPDATE agents_meta SET last_heartbeat_at = now() - interval '20 seconds' "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()

        assert aid not in dict(
            _select_idle_agents_needing_heartbeat(pool, _THRESHOLD_S, heartbeat_interval_s=300.0)
        )
        assert aid in dict(
            _select_idle_agents_needing_heartbeat(pool, _THRESHOLD_S, heartbeat_interval_s=15.0)
        )

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


# ───────────── consecutive-failure backoff (Task #1928) ─────────────


class TestConsecutiveFailureBackoff:
    """A check-in that produces no LLM turn is a failed check-in (the 3962
    context-overflow case: the daemon poked a permanently-rejecting agent
    ~1150 times). The daemon spaces streaking agents by `2^streak` idle
    windows; a real turn resets the streak."""

    def test_backoff_skips_agent_until_deadline(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        aid = _make_idle(db_conn, status_changed_s_ago=400)
        # Deadline in the future -> excluded; deadline passed -> selected.
        assert aid not in dict(
            _select_idle_agents_needing_heartbeat(
                pool, _THRESHOLD_S, backoff_until={aid: time.time() + 1000}
            )
        )
        assert aid in dict(
            _select_idle_agents_needing_heartbeat(
                pool, _THRESHOLD_S, backoff_until={aid: time.time() - 1}
            )
        )

    def test_backed_off_agent_does_not_consume_limit_slots(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """The limit applies AFTER the backoff filter: a backed-off agent must
        not occupy a per-step wake-rate slot that a healthy due agent needs."""
        healthy = _make_idle(db_conn, status_changed_s_ago=900)
        wedged = _make_idle(db_conn, status_changed_s_ago=700)

        selected = _select_idle_agents_needing_heartbeat(
            pool,
            _THRESHOLD_S,
            limit=1,
            backoff_until={wedged: time.time() + 10_000},
        )
        assert [r[0] for r in selected] == [healthy]

    def test_reconcile_increments_streak_when_checkin_produced_no_turn(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """The check-in was sent when the agent had been idle ~6 minutes; a
        cycle later `last_active_at` has not moved (no turn ran) -> streak 1."""
        aid = _make_idle(db_conn, status_changed_s_ago=400)
        pending: dict[int, float] = {aid: 6.0}
        streaks: dict[int, int] = {}

        _reconcile_checkin_outcomes(
            pool, pending_checkin=pending, failure_streak=streaks, idle_threshold_s=_THRESHOLD_S
        )

        assert pending == {}
        assert streaks == {aid: 1}

    def test_reconcile_resets_streak_when_checkin_produced_a_turn(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """The check-in produced a turn (`last_active_at` advanced) -> streak
        reset (stays empty / drops)."""
        aid = _make_idle(db_conn, status_changed_s_ago=400)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET last_active_at = now() WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        pending: dict[int, float] = {aid: 6.0}
        streaks: dict[int, int] = {aid: 3}

        _reconcile_checkin_outcomes(
            pool, pending_checkin=pending, failure_streak=streaks, idle_threshold_s=_THRESHOLD_S
        )

        assert streaks == {}, "a turn after the check-in must clear the streak"

    def test_reconcile_resets_streak_on_fresh_activity_without_pending(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """No check-in was sent this cycle (backoff active), but a real wake
        produced a turn — fresh `last_active_at` clears the streak so the
        recovered agent is probed again at the normal cadence."""
        aid = _make_idle(db_conn, status_changed_s_ago=400)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET last_active_at = now() WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        streaks: dict[int, int] = {aid: 4}

        _reconcile_checkin_outcomes(
            pool, pending_checkin={}, failure_streak=streaks, idle_threshold_s=_THRESHOLD_S
        )

        assert streaks == {}

    def test_reconcile_stops_tracking_agents_outside_daemon_lanes(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        aid = _make_idle(db_conn, status_changed_s_ago=400)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (aid,))
        db_conn.commit()
        streaks: dict[int, int] = {aid: 2}

        _reconcile_checkin_outcomes(
            pool, pending_checkin={}, failure_streak=streaks, idle_threshold_s=_THRESHOLD_S
        )

        assert streaks == {}

    def test_backoff_deadlines_exponential_capped(self) -> None:
        """streak=1 doubles the normal interval; the growth caps at
        _BACKOFF_MAX_WINDOWS so the daemon still probes a wedged agent."""
        from services.heartbeat.daemon import _BACKOFF_MAX_WINDOWS

        d1 = _backoff_deadlines({1: 1}, _THRESHOLD_S)
        d2 = _backoff_deadlines({1: 2}, _THRESHOLD_S)
        d10 = _backoff_deadlines({1: 10}, _THRESHOLD_S)
        assert d1[1] - time.time() == pytest.approx(2 * _THRESHOLD_S, abs=1.0)  # pyright: ignore[reportUnknownMemberType]
        assert d2[1] - d1[1] == pytest.approx(2 * _THRESHOLD_S, abs=1.0)  # pyright: ignore[reportUnknownMemberType]
        assert d10[1] - d2[1] == pytest.approx((_BACKOFF_MAX_WINDOWS - 4) * _THRESHOLD_S, abs=1.0)  # pyright: ignore[reportUnknownMemberType]


def _mirror_event(agent_id: int, event_name: str) -> dict | None:
    """The latest mirror line for (agent, event), or None while the drain
    thread has not landed it."""
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
        if obj.get("event_name") == event_name and obj.get("agent_id") == agent_id:
            return obj
    return None


def _poll_mirror(agent_id: int, event_name: str, timeout_s: float = 2.0) -> dict | None:
    deadline = time.monotonic() + timeout_s
    ev = None
    while time.monotonic() < deadline:
        telemetry.flush()
        ev = _mirror_event(agent_id, event_name)
        if ev is not None:
            break
        time.sleep(0.05)
    return ev


class TestNudgeBackoffB7:
    """Platform-side nudge backoff: consecutive no-op nudges stretch the
    reminder floor by 2^level (cap 24h); real inbound or a pause resets."""

    def _set_level(self, db_conn: psycopg.Connection, aid: int, level: int) -> None:
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET heartbeat_backoff_level = %s WHERE id = %s",
                (level, aid),
            )
        db_conn.commit()

    def test_select_stretches_reminder_floor_by_level(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """last_heartbeat_at 10 min ago is due at the default 5 min cadence
        but not at level 2 (5 min * 4 = 20 min)."""
        aid = _make_idle(db_conn, status_changed_s_ago=400)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET last_heartbeat_at = now() - make_interval(secs => 600) "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert aid in _selected(pool)
        self._set_level(db_conn, aid, 2)
        assert aid not in _selected(pool)

    def test_reconcile_raises_level_after_n_consecutive_noops(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        aid = _make_idle(db_conn, status_changed_s_ago=400)
        noop: dict[int, int] = {aid: 2}

        _reconcile_checkin_outcomes(
            pool,
            pending_checkin={aid: 6.0},
            failure_streak={},
            idle_threshold_s=_THRESHOLD_S,
            noop_streak=noop,
            heartbeat_interval_s=_THRESHOLD_S,
            noop_nudges_threshold=3,
        )

        assert noop == {aid: 0}
        with db_conn.cursor() as cur:
            cur.execute("SELECT heartbeat_backoff_level FROM agents_meta WHERE id = %s", (aid,))
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 1
        ev = _poll_mirror(aid, "heartbeat_backoff_raised")
        assert ev is not None
        assert ev["attributes"]["level"] == 1
        assert ev["attributes"]["interval_seconds"] == 600

    def test_reconcile_clears_streak_on_real_inbound(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        aid = _make_idle(db_conn, status_changed_s_ago=400)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET last_heartbeat_at = now() - make_interval(secs => 60) "
                "WHERE id = %s",
                (aid,),
            )
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (%s, 'hi', 'chat', 'user')",
                (aid,),
            )
        db_conn.commit()
        noop: dict[int, int] = {aid: 2}

        _reconcile_checkin_outcomes(
            pool,
            pending_checkin={},
            failure_streak={},
            idle_threshold_s=_THRESHOLD_S,
            noop_streak=noop,
            heartbeat_interval_s=_THRESHOLD_S,
            noop_nudges_threshold=3,
        )

        assert noop == {}

    def test_reconcile_clears_streak_on_pause(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        aid = _make_idle(db_conn, status_changed_s_ago=400, paused_until_s_ahead=3600)
        noop: dict[int, int] = {aid: 2}

        _reconcile_checkin_outcomes(
            pool,
            pending_checkin={},
            failure_streak={},
            idle_threshold_s=_THRESHOLD_S,
            noop_streak=noop,
            heartbeat_interval_s=_THRESHOLD_S,
            noop_nudges_threshold=3,
        )

        assert noop == {}

    def test_raise_is_capped_at_24h_max_level(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        from services.heartbeat.daemon import _backoff_max_level

        aid = _make_idle(db_conn, status_changed_s_ago=400)
        max_level = _backoff_max_level(_THRESHOLD_S)
        self._set_level(db_conn, aid, max_level)
        noop: dict[int, int] = {aid: 2}

        _reconcile_checkin_outcomes(
            pool,
            pending_checkin={aid: 6.0},
            failure_streak={},
            idle_threshold_s=_THRESHOLD_S,
            noop_streak=noop,
            heartbeat_interval_s=_THRESHOLD_S,
            noop_nudges_threshold=3,
        )

        with db_conn.cursor() as cur:
            cur.execute("SELECT heartbeat_backoff_level FROM agents_meta WHERE id = %s", (aid,))
            row = cur.fetchone()
            assert row is not None
            assert row[0] == max_level

    def test_sweep_resets_level_on_real_inbound(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        aid = _make_idle(db_conn, status_changed_s_ago=400)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET heartbeat_backoff_level = 2, "
                "last_heartbeat_at = now() - make_interval(secs => 60) WHERE id = %s",
                (aid,),
            )
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (%s, 'hi', 'chat', 'user')",
                (aid,),
            )
        db_conn.commit()

        _sweep_backoff_resets(pool)

        with db_conn.cursor() as cur:
            cur.execute("SELECT heartbeat_backoff_level FROM agents_meta WHERE id = %s", (aid,))
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 0
        ev = _poll_mirror(aid, "heartbeat_backoff_reset")
        assert ev is not None
        assert ev["attributes"]["previous_level"] == 2
        assert ev["attributes"]["reason"] == "real_inbound"

    def test_sweep_leaves_level_without_engagement(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        aid = _make_idle(db_conn, status_changed_s_ago=400)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET heartbeat_backoff_level = 2, last_heartbeat_at = now() "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()

        _sweep_backoff_resets(pool)

        with db_conn.cursor() as cur:
            cur.execute("SELECT heartbeat_backoff_level FROM agents_meta WHERE id = %s", (aid,))
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 2
        assert _poll_mirror(aid, "heartbeat_backoff_reset", timeout_s=0.5) is None
