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

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from services.wake.heartbeat.daemon import (
    _select_idle_agents_needing_heartbeat,
)
from services.wake.heartbeat.tests.heartbeat_support import (
    _THRESHOLD_S,
    _lease,
    _make_idle,
)
from services.wake.heartbeat.tests.heartbeat_support import pool as pool


def _selected(pool: ConnectionPool) -> dict[int, float]:
    """agent_id -> idle_minutes for every agent the daemon would check in on."""
    return dict(_select_idle_agents_needing_heartbeat(pool, _THRESHOLD_S))


class TestSelectIdleAgents:
    def test_idle_past_threshold_unpaused_is_selected(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        selected = _selected(pool)
        assert aid in selected
        assert float(selected[aid]) == pytest.approx(400 / 60.0, abs=0.5)  # pyright: ignore[reportUnknownMemberType]

    def test_idling_without_a_lease_is_selected(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """An idle hosted agent has no turn lease and can still receive a check-in."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=_THRESHOLD_S + 60,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET lease_expires_at = NULL, pid = NULL WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert aid in _selected(pool)

    def test_idle_under_threshold_unpaused_excluded(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """Scenario 1: a recent wake (status_changed_at fresh) leaves the agent
        under the idle threshold, so it is not yet due."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=120,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        assert aid not in _selected(pool)

    def test_running_agent_excluded(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """Only idling agents get a check-in; a running one is active by definition."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            status="running",
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        assert aid not in _selected(pool)

    def test_pending_inbound_excluded(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """An agent about to wake on a real message does not also need a check-in."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (%s, 'hi', 'chat', 'user')",
                (aid,),
            )
        db_conn.commit()
        assert aid not in _selected(pool)

    @pytest.mark.parametrize("status", ["requested", "accepted"])
    def test_agent_under_open_impersonation_lease_excluded(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        status: str,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """Task #4872: while a lease is preparing, neither executor can
        receive a check-in, so it could not
        produce a turn and must not be queued (it would only age into a false
        delivery poison)."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        _lease(db_conn, aid, status=status)
        assert aid not in _selected(pool)

    def test_terminal_impersonation_lease_does_not_block_checkin(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """A lease with nothing left to apply is history: the native loop is
        the consumer again and the check-in cadence resumes."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        _lease(db_conn, aid, status="expired")
        assert aid in _selected(pool)

    def test_unapplied_impersonation_delta_excludes_agent(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """Unapplied delta state counts as in-flight (the impersonation
        subsystem's own predicate): stay silent until the native catches up."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        _lease(db_conn, aid, status="expired", delta_version=3, applied_version=2)
        assert aid not in _selected(pool)

    def test_unapplied_automatic_handoff_excludes_agent(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """An automatic lease whose handoff was never applied still counts as
        in-flight; a check-in waits for the application."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        _lease(db_conn, aid, status="expired", automatic=True)
        assert aid not in _selected(pool)

    def test_applied_automatic_handoff_does_not_block_checkin(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        _lease(db_conn, aid, status="expired", automatic=True, handoff_applied=True)
        assert aid in _selected(pool)

    def test_active_pause_suppresses_even_when_idle_past_threshold(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """Scenario 2: while the pause window is in the future, the agent is
        skipped even though it has been idle far longer than the threshold."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=600,
            paused_until_s_ahead=1800,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        assert aid not in _selected(pool)

    def test_real_turn_during_pause_delays_checkin_past_window(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """R-6: the pause window is a floor, not an absolute check-in time. A
        real turn during the window starts the normal idle clock, so after expiry
        the agent must wait the idle threshold instead of taking a wasted wake."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=10,
            last_active_s_ago=10,
            paused_until_s_ahead=-1,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        assert aid not in _selected(pool)

    def test_open_pause_window_still_suppresses_checkin_after_real_turn(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """The R-6 pause floor wins while it is open, then the real turn's
        idle clock wins after expiry; both edges are one unified due-time rule."""
        open_window = _make_idle(
            db_conn,
            status_changed_s_ago=10,
            last_active_s_ago=10,
            paused_until_s_ahead=60,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        expired_window = _make_idle(
            db_conn,
            status_changed_s_ago=10,
            last_active_s_ago=10,
            paused_until_s_ahead=-1,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        selected = _selected(pool)
        assert open_window not in selected
        assert expired_window not in selected

    def test_pause_expired_before_last_wake_uses_normal_clock(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """Once a wake post-dates the expired pause window (status_changed_at >
        heartbeat_paused_until), the normal wake-resettable clock resumes: an
        agent idle only 120s is under the threshold and not yet due, NOT stuck
        firing forever on the stale past window."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=120,
            paused_until_s_ahead=-300,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        assert aid not in _selected(pool)

    def test_pause_expired_after_long_idle_is_due(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """No intervening wake: the agent has been idle 600s and its pause
        window expired 60s ago. It is overdue under both regimes and selected."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=600,
            paused_until_s_ahead=-60,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        assert aid in _selected(pool)


class TestIdleClockCountsRealActivityOnly:
    """The idle clock is last_active_at (real work), not status_changed_at (bumped
    by every status flip). These pin the semantic fix: an ops restart resets
    status_changed_at without a real turn and must NOT reset the idle clock."""

    def test_ops_restart_does_not_reset_idle_clock(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """An agent idle 400s (past threshold) then hit by an ops restart:
        status_changed_at is fresh (5s ago, the re-idle after respawn) but
        last_active_at is still 400s ago (the ops cycle ran no LLM turn). It stays
        due — the rollout did not zero its idle timer."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=5,
            last_active_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        assert aid in _selected(pool)

    def test_ops_restart_does_not_make_long_idle_agent_look_fresh(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """Contrast with the pre-fix behavior: keyed off status_changed_at the same
        agent (fresh status_changed_at) would read as only 5s idle and be excluded.
        Keyed off last_active_at it is correctly overdue. The idle_minutes reported
        reflects the real 400s, not the 5s since the ops re-idle."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=5,
            last_active_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        selected = _selected(pool)
        assert aid in selected
        assert float(selected[aid]) == pytest.approx(400 / 60.0, abs=0.5)  # pyright: ignore[reportUnknownMemberType]

    def test_recent_real_turn_resets_idle_clock(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """The mirror case: an agent whose status_changed_at is old (600s) but that
        just completed a real turn 30s ago (last_active_at fresh) is NOT due — real
        activity, unlike ops churn, does reset the idle clock."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=600,
            last_active_s_ago=30,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
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
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """With jitter, the unpaused due-time is `threshold + (id mod span)`. An
        agent idle just short of its own jittered due-time is excluded; the same
        agent idle just past it is selected. Keyed on the real id so the offset is
        exact, not probabilistic."""
        span, threshold = 100.0, 300.0
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=1,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        offset = aid % int(span)

        self._backdate(db_conn, aid, threshold + offset - 5)
        got = dict(_select_idle_agents_needing_heartbeat(pool, threshold, jitter_span_s=span))
        assert aid not in got, "idle just short of the jittered due-time must be excluded"

        self._backdate(db_conn, aid, threshold + offset + 5)
        got = dict(_select_idle_agents_needing_heartbeat(pool, threshold, jitter_span_s=span))
        assert aid in got, "idle just past the jittered due-time must be selected"

    def test_zero_jitter_matches_plain_threshold(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """jitter_span_s=0 (the default) collapses the offset to 0 — no
        divide-by-zero, identical to the un-jittered predicate."""
        aid = _make_idle(
            db_conn,
            status_changed_s_ago=350,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        got = dict(_select_idle_agents_needing_heartbeat(pool, 300.0, jitter_span_s=0.0))
        assert aid in got

    def test_limit_caps_batch_oldest_idle_first(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """`limit` bounds the batch; ordering is oldest-idle first so the most
        overdue agents drain ahead of fresher ones."""
        a_old = _make_idle(
            db_conn,
            status_changed_s_ago=900,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        a_mid = _make_idle(
            db_conn,
            status_changed_s_ago=600,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        a_new = _make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )

        order = [r[0] for r in _select_idle_agents_needing_heartbeat(pool, _THRESHOLD_S)]
        assert order.index(a_old) < order.index(a_mid) < order.index(a_new)

        capped = _select_idle_agents_needing_heartbeat(pool, _THRESHOLD_S, limit=2)
        assert len(capped) == 2
        assert a_new not in [r[0] for r in capped]
