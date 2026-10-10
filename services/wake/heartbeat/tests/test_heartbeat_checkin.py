"""Heartbeat dispatch, emitted events, and consecutive-failure backoff."""

from __future__ import annotations

import asyncio
import time

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base import telemetry
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.redis_listener import RedisInboundListener
from base.lm.catalog import ModelCatalog
from services.wake.heartbeat.daemon import (
    _backoff_deadlines,
    _reconcile_checkin_outcomes,
    _select_idle_agents_needing_heartbeat,
    _send_heartbeat_checkin,
)
from services.wake.heartbeat.tests.heartbeat_support import _THRESHOLD_S, _make_idle
from services.wake.heartbeat.tests.heartbeat_support import pool as pool
from tests.fixtures.units import spawn_agent


def _selected(pool: ConnectionPool) -> dict[int, float]:
    """agent_id -> idle_minutes for every agent the daemon would check in on."""
    return dict(_select_idle_agents_needing_heartbeat(pool, _THRESHOLD_S))


def _mirror_nudged(agent_id: int) -> tuple[str, str, int] | None:
    """The latest heartbeat_nudged mirror line for `agent_id` — (event_name,
    level, idle_minutes), or None while the drain thread has not landed it."""
    import json
    from datetime import UTC, datetime

    from base.paths import logs_dir

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
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        aid = spawn_agent(
            spawner="user",
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
        )
        _send_heartbeat_checkin(pool, database, event_bus, aid, 7.0)
        # The emitter drains asynchronously (0.5s cadence) — flush() can
        # race the drain thread for the queue, so poll briefly for the line.
        # The PG events copy was retired at the LGTM cutover (task #1197 close-C)
        # and later dropped; the durable local copy is the JSONL mirror.
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
            content
            == "Heartbeat. Review your existing responsibilities and continue actionable work. "
            "For a known wait, pause your heartbeat; if your role is complete, end it."
        )  # idle-minutes detail lives in the event row, not the content (0064)
        assert ev is not None
        assert ev[0] == "heartbeat_nudged"
        assert ev[1] == "info"
        assert int(ev[2]) == 7

    def test_consumed_heartbeat_defers_the_next_checkin(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """A completed check-in must start a durable reminder interval.

        Regression for #5759: a permanent provider rejection leaves
        ``last_active_at`` unchanged. Once its heartbeat inbound is consumed,
        the pending-inbound guard no longer applies, so the daemon must still
        hold the agent until the configured heartbeat interval has elapsed.
        """
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        _send_heartbeat_checkin(pool, database, event_bus, aid, 7.0)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status = 'done' WHERE agent_id = %s", (aid,))
        db_conn.commit()

        assert aid not in _selected(pool)

    def test_reminder_uses_heartbeat_interval_not_dispatch_step(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """The 15-second dispatcher step must not become the reminder cadence."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        _send_heartbeat_checkin(pool, database, event_bus, aid, 7.0)
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
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """The check-in publishes a Redis wake to the target agent's channel so an
        idle agent runs the heartbeat turn now instead of at its next SELECT
        recheck — park a per-agent listener, fire the check-in, assert it wakes."""
        aid = spawn_agent(
            spawner="user",
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
        )
        listener = RedisInboundListener(settings.data_plane.redis_url, aid)
        try:
            wait_task = asyncio.create_task(listener.wait_one(timeout=10.0))
            await asyncio.sleep(0.2)  # let the subscribe take effect before the publish
            t0 = time.monotonic()
            await asyncio.to_thread(_send_heartbeat_checkin, pool, database, event_bus, aid, 7.0)
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
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
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
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """The limit applies AFTER the backoff filter: a backed-off agent must
        not occupy a per-step wake-rate slot that a healthy due agent needs."""
        healthy = _make_idle(
            db_conn,
            status_changed_s_ago=900,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        wedged = _make_idle(
            db_conn,
            status_changed_s_ago=700,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )

        selected = _select_idle_agents_needing_heartbeat(
            pool,
            _THRESHOLD_S,
            limit=1,
            backoff_until={wedged: time.time() + 10_000},
        )
        assert [r[0] for r in selected] == [healthy]

    @pytest.mark.parametrize("advanced", [False, True])
    def test_real_selected_idle_clock_survives_next_checkin_cycle(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        advanced: bool,
        database: Database,
        event_bus: EventBus,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """Use the actual PG result in pending state, as the dispatch loop does."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=1200,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        pending = _selected(pool)
        _send_heartbeat_checkin(pool, database, event_bus, aid, pending[aid])
        if advanced:
            # Still beyond the idle threshold: recovery must use the observed
            # progress, not the independent fresh-activity shortcut.
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET last_active_at = now() - interval '10 minutes' "
                    "WHERE id = %s",
                    (aid,),
                )
            db_conn.commit()
        streaks: dict[int, int] = {aid: 2}
        _reconcile_checkin_outcomes(
            pool, pending_checkin=pending, failure_streak=streaks, idle_threshold_s=_THRESHOLD_S
        )
        assert pending == {}
        assert streaks == ({} if advanced else {aid: 3})

    def test_reconcile_increments_streak_when_checkin_produced_no_turn(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """The check-in was sent when the agent had been idle ~6 minutes; a
        cycle later `last_active_at` has not moved (no turn ran) -> streak 1."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
        pending: dict[int, float] = {aid: 6.0}
        streaks: dict[int, int] = {}

        _reconcile_checkin_outcomes(
            pool, pending_checkin=pending, failure_streak=streaks, idle_threshold_s=_THRESHOLD_S
        )

        assert pending == {}
        assert streaks == {aid: 1}

    def test_reconcile_resets_streak_when_checkin_produced_a_turn(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """The check-in produced a turn (`last_active_at` advanced) -> streak
        reset (stays empty / drops)."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
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
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """No check-in was sent this cycle (backoff active), but a real wake
        produced a turn — fresh `last_active_at` clears the streak so the
        recovered agent is probed again at the normal cadence."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
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
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
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
        from services.wake.heartbeat.daemon import _BACKOFF_MAX_WINDOWS

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

    from base.paths import logs_dir

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
