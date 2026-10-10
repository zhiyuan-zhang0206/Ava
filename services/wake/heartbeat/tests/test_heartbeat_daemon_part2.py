"""`services.wake.heartbeat.daemon` — idle-agent check-in selection + the pause window.

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

import time

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base import telemetry
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from services.wake.heartbeat.daemon import (
    _reconcile_checkin_outcomes,
    _select_idle_agents_needing_heartbeat,
    _sweep_backoff_resets,
)
from tests.fixtures.units import spawn_agent

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
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
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
    aid = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
    )
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


# ───────────── consecutive-failure backoff (Task #1928) ─────────────


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
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """last_heartbeat_at 10 min ago is due at the default 5 min cadence
        but not at level 2 (5 min * 4 = 20 min)."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
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
            paused_until_s_ahead=3600,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
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
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        from services.wake.heartbeat.daemon import _backoff_max_level

        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
        )
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
