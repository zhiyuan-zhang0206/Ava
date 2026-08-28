"""Agent hibernation — the ops-layer memory swap-out / swap-in mechanism.

Covers the pieces that make hibernation safe and invisible:
- `swap_in_agent` (ops.agents): the clean-restart wake — CAS 'hibernating' ->
  unclaimed 'idling', clears pid, launches, and inserts NO lifecycle inbound (the agent
  must not be able to tell it was ever swapped out).
- `mark_agent_hibernating_op` (ops.ops_lifecycle): the swap-out finalize — parks
  'running'/'idling' -> 'hibernating', leaves pages OPEN, writes no exit event;
  leaves 'restarting'/'terminated' alone.
- `HibernateController` (ops.controllers.hibernate): the machine-scoped scans —
  swap-out selects idle-past-threshold agents with no pending inbound and signals
  SIGUSR1; swap-in selects hibernating agents with a pending inbound and relaunches
  them; the `hibernate_enabled` gate stops swap-out but never swap-in.
- reaper immunity: a 'hibernating' row with a dead pid is invisible to all three
  respawn reapers, so the mechanism is not torn down as a corpse (locks the
  "reapers unchanged" invariant the design depends on).

Launch is stubbed by the autouse `_guard_agent_launch` (no real process forks);
tests that assert a launch happened read the `launched_agents` recorder.
"""

from __future__ import annotations

import signal

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ops.agent_identity import AgentProcessIdentity
from ops.agents import swap_in_agent
from ops.controllers import hibernate as hib
from ops.controllers import respawn as rd
from ops.ops_lifecycle import mark_agent_hibernating_op
from services.heartbeat.daemon import _send_heartbeat_checkin
from shared.config import settings
from shared.machine import machine_name
from tests.conftest import spawn_agent

_LIVE_PID = 999001  # a stand-in pid; os.kill is monkeypatched, never really signalled
_DEAD_PID = 424242


def _stub_identity(
    monkeypatch: pytest.MonkeyPatch, module: object, verdict: AgentProcessIdentity
) -> None:
    """Force `module`'s identity probe to return `verdict` for every pid.

    Both the swap-out and the reapers now decide on process identity, so the
    stand-in pids above resolve to nothing real and every such test must say which
    verdict it is exercising. Tests asserting that NO signal was sent stub `OWNED`
    on purpose — otherwise a probe failure would satisfy the assertion and the
    thing under test (the warm-pool floor, the `hibernate_enabled` gate) would go
    unexercised."""
    monkeypatch.setattr(module, "probe_agent_process", lambda _pid, _agent_id: verdict)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]


@pytest.fixture
def sync_pool():
    pool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield pool
    finally:
        pool.close()


def _park(
    db: psycopg.Connection,
    *,
    status: str,
    pid: int | None = None,
    idle_s_ago: float = 0.0,
    machine: str | None = None,
) -> int:
    """Spawn an agent row and force it into `status`, backdating last_active_at by
    `idle_s_ago` and optionally overriding pid / machine. Returns the agent id."""
    aid = spawn_agent(spawner="user")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = %s, "
            "last_active_at = now() - make_interval(secs => %s), "
            "lease_expires_at = now() + make_interval(secs => 600) "
            "WHERE id = %s",
            (status, pid, idle_s_ago, aid),
        )
        if machine is not None:
            cur.execute("UPDATE agents_meta SET machine = %s WHERE id = %s", (machine, aid))
    db.commit()
    return aid


def _add_pending_inbound(db: psycopg.Connection, aid: int, kind: str = "heartbeat") -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, %s, %s, 'system')",
            (aid, "Heartbeat.", kind),
        )
    db.commit()


def _status(db: psycopg.Connection, aid: int) -> str:
    with db.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (aid,))
        return cur.fetchone()[0]  # type: ignore[index]


def _pid(db: psycopg.Connection, aid: int) -> int | None:
    with db.cursor() as cur:
        cur.execute("SELECT pid FROM agents_meta WHERE id = %s", (aid,))
        return cur.fetchone()[0]  # type: ignore[index]


def _inbound_kinds(db: psycopg.Connection, aid: int) -> list[str]:
    with db.cursor() as cur:
        cur.execute("SELECT kind FROM inbound_messages WHERE agent_id = %s", (aid,))
        return [r[0] for r in cur.fetchall()]


# ── swap_in_agent (clean-restart wake) ──────────────────────────────────────


class TestSwapInAgent:
    def test_hibernating_flips_to_unclaimed_idling_clears_pid_and_launches(
        self,
        db_conn: psycopg.Connection,
        launched_agents: list,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    ) -> None:
        aid = _park(db_conn, status="hibernating", pid=_DEAD_PID)
        launched_agents.clear()  # drop the spawn-setup launch; only track the swap-in

        assert swap_in_agent(aid) is True

        assert _status(db_conn, aid) == "idling"  # launch stubbed, no claim yet
        assert _pid(db_conn, aid) is None  # stale pre-hibernation pid cleared
        assert [c.agent_id for c in launched_agents] == [aid]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def test_inserts_no_lifecycle_inbound(
        self,
        db_conn: psycopg.Connection,
        launched_agents: list,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    ) -> None:
        """The whole point of hibernation invisibility: the wake inserts NO inbound
        (no resurrect marker, no restart_completed) — only the pending inbound that
        triggered it survives, exactly as a never-swapped idle agent would see."""
        aid = _park(db_conn, status="hibernating", pid=_DEAD_PID)
        _add_pending_inbound(db_conn, aid, kind="heartbeat")

        swap_in_agent(aid)

        # Only the heartbeat that triggered the wake — no 'resurrect' / 'restart_completed' / 'fork'.
        assert _inbound_kinds(db_conn, aid) == ["heartbeat"]

    def test_not_hibernating_is_noop(
        self,
        db_conn: psycopg.Connection,
        launched_agents: list,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    ) -> None:
        """CAS guard: a non-hibernating row is never swapped in (no double-launch)."""
        aid = _park(db_conn, status="idling", pid=_LIVE_PID)
        launched_agents.clear()  # drop the spawn-setup launch
        assert swap_in_agent(aid) is False
        assert _status(db_conn, aid) == "idling"
        assert launched_agents == []

    def test_double_swap_in_only_one_launches(
        self,
        db_conn: psycopg.Connection,
        launched_agents: list,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    ) -> None:
        """Idempotent under a double-trigger (e.g. a chat and a heartbeat land in
        the same tick, both routed to swap-in): the CAS `WHERE status='hibernating'`
        picks a single winner — the first flips it to unclaimed 'idling' and launches, the
        second sees a non-hibernating row and no-ops. Exactly one launch."""
        aid = _park(db_conn, status="hibernating", pid=_DEAD_PID)
        launched_agents.clear()

        first = swap_in_agent(aid)
        second = swap_in_agent(aid)  # the row is now 'idling' — loses the CAS

        assert (first, second) == (True, False)
        assert [c.agent_id for c in launched_agents] == [  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            aid
        ]  # exactly one launch  # pyright: ignore[reportUnknownMemberType]


# ── mark_agent_hibernating_op (swap-out finalize) ────────────────────────────


class TestMarkAgentHibernatedOp:
    async def test_idling_parks_hibernating(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        aid = _park(db_conn, status="idling", pid=_DEAD_PID)
        await mark_agent_hibernating_op(aid, sync_pool)
        assert _status(db_conn, aid) == "hibernating"

    async def test_running_parks_hibernating(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """The idle-wait's own finally flips IDLING->RUNNING as SIGUSR1 unwinds, so
        the finalize must accept 'running' too (same as mark_agent_exited_op)."""
        aid = _park(db_conn, status="running", pid=_DEAD_PID)
        await mark_agent_hibernating_op(aid, sync_pool)
        assert _status(db_conn, aid) == "hibernating"

    async def test_restarting_is_left_untouched(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """A concurrent restart's 'restarting' must survive — the restarter owns it."""
        aid = _park(db_conn, status="restarting", pid=_DEAD_PID)
        await mark_agent_hibernating_op(aid, sync_pool)
        assert _status(db_conn, aid) == "restarting"

    async def test_terminated_is_left_untouched(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        aid = _park(db_conn, status="terminated", pid=_DEAD_PID)
        await mark_agent_hibernating_op(aid, sync_pool)
        assert _status(db_conn, aid) == "terminated"

    async def test_pages_stay_open(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """Unlike /exited (which closes show() pages), hibernation keeps them open —
        the cascade_close_agent_pages trigger fires only on 'terminated'."""
        aid = _park(db_conn, status="idling", pid=_DEAD_PID)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_pages (agent_id, name, port) VALUES (%s, 'work', 8765)",
                (aid,),
            )
        db_conn.commit()

        await mark_agent_hibernating_op(aid, sync_pool)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT closed_at FROM agent_pages WHERE agent_id = %s AND name = 'work'",
                (aid,),
            )
            assert cur.fetchone()[0] is None  # type: ignore[index]


class TestSwapOutSelection:
    def test_idle_past_threshold_no_pending_is_selected(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        aid = _park(db_conn, status="idling", pid=_LIVE_PID, idle_s_ago=120)
        rows = hib._select_local_swap_out_candidates(sync_pool, machine_name(), 60.0, 0)
        assert (aid, _LIVE_PID) in rows

    def test_idle_under_threshold_excluded(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        aid = _park(db_conn, status="idling", pid=_LIVE_PID, idle_s_ago=30)
        rows = hib._select_local_swap_out_candidates(sync_pool, machine_name(), 60.0, 0)
        assert aid not in [r[0] for r in rows]

    def test_preclaim_idling_without_pid_is_not_selected(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """The shared idling status is not enough for swap-out: an unclaimed
        birth row has no process to signal and must stay out of this selector."""
        aid = _park(db_conn, status="idling", pid=None, idle_s_ago=120)
        rows = hib._select_local_swap_out_candidates(sync_pool, machine_name(), 60.0, 0)
        assert aid not in [r[0] for r in rows]

    def test_pending_inbound_excluded(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """An agent about to wake on a queued message must not be swapped out."""
        aid = _park(db_conn, status="idling", pid=_LIVE_PID, idle_s_ago=120)
        _add_pending_inbound(db_conn, aid)
        rows = hib._select_local_swap_out_candidates(sync_pool, machine_name(), 60.0, 0)
        assert aid not in [r[0] for r in rows]

    def test_running_excluded(self, db_conn: psycopg.Connection, sync_pool: ConnectionPool) -> None:
        """Only 'idling' is swapped out — never an in-flight turn."""
        aid = _park(db_conn, status="running", pid=_LIVE_PID, idle_s_ago=120)
        rows = hib._select_local_swap_out_candidates(sync_pool, machine_name(), 60.0, 0)
        assert aid not in [r[0] for r in rows]

    def test_other_machine_excluded(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """Machine-scoped: only this host signals its own agents' processes."""
        aid = _park(db_conn, status="idling", pid=_LIVE_PID, idle_s_ago=120, machine="other-box")
        rows = hib._select_local_swap_out_candidates(sync_pool, machine_name(), 60.0, 0)
        assert aid not in [r[0] for r in rows]


class TestSwapOutWarmPoolFloor:
    """`min_active` exempts this host's N most recently active agents from
    swap-out, ranked by `last_active_at` among 'running'/'idling' only."""

    def test_most_recently_active_within_floor_excluded(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """floor=1: the more-recently-active of two otherwise-eligible idle agents
        stays resident; the older one is still selected."""
        newer = _park(db_conn, status="idling", pid=_LIVE_PID, idle_s_ago=120)
        older = _park(db_conn, status="idling", pid=_LIVE_PID + 1, idle_s_ago=600)
        rows = hib._select_local_swap_out_candidates(sync_pool, machine_name(), 60.0, 1)
        ids = [r[0] for r in rows]
        assert newer not in ids  # protected by the floor
        assert older in ids  # outside the floor, still eligible

    def test_oldest_last_active_selected_first_beyond_floor(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """floor=2 over three idle-past-threshold agents: only the single agent
        outside the floor (the least recently active) is a candidate."""
        newest = _park(db_conn, status="idling", pid=_LIVE_PID, idle_s_ago=120)
        middle = _park(db_conn, status="idling", pid=_LIVE_PID + 1, idle_s_ago=300)
        oldest = _park(db_conn, status="idling", pid=_LIVE_PID + 2, idle_s_ago=600)
        rows = hib._select_local_swap_out_candidates(sync_pool, machine_name(), 60.0, 2)
        assert [r[0] for r in rows] == [oldest]
        assert newest not in [r[0] for r in rows]
        assert middle not in [r[0] for r in rows]

    def test_terminated_does_not_occupy_a_floor_slot(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """A 'terminated' row with a very recent last_active_at must not count
        toward the floor and push a genuinely active agent out of protection —
        the floor ranks only 'running'/'idling'. With floor=1 and only one
        running/idling agent, that agent is the sole floor occupant and must stay
        protected regardless of the more-recently-active terminated row."""
        _park(db_conn, status="terminated", pid=None, idle_s_ago=0)
        idling = _park(db_conn, status="idling", pid=_LIVE_PID, idle_s_ago=120)
        rows = hib._select_local_swap_out_candidates(sync_pool, machine_name(), 60.0, 1)
        assert idling not in [r[0] for r in rows]

    def test_hibernating_does_not_occupy_a_floor_slot(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """Same as terminated: 'hibernating' is not in the ranked window."""
        _park(db_conn, status="hibernating", pid=_DEAD_PID, idle_s_ago=0)
        idling = _park(db_conn, status="idling", pid=_LIVE_PID, idle_s_ago=120)
        rows = hib._select_local_swap_out_candidates(sync_pool, machine_name(), 60.0, 1)
        assert idling not in [r[0] for r in rows]

    def test_floor_zero_is_unrestricted(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """min_active=0 selects every otherwise-eligible agent — the pre-floor
        behavior, including the single most-recently-active one."""
        aid = _park(db_conn, status="idling", pid=_LIVE_PID, idle_s_ago=120)
        rows = hib._select_local_swap_out_candidates(sync_pool, machine_name(), 60.0, 0)
        assert aid in [r[0] for r in rows]


class TestSwapInSelection:
    def test_hibernating_with_pending_is_selected(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        aid = _park(db_conn, status="hibernating")
        _add_pending_inbound(db_conn, aid)
        assert aid in hib._select_local_swap_in_candidates(sync_pool, machine_name())

    def test_hibernating_without_pending_excluded(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """No inbound waiting -> nothing to wake for; stays parked until nudged."""
        aid = _park(db_conn, status="hibernating")
        assert aid not in hib._select_local_swap_in_candidates(sync_pool, machine_name())

    def test_other_machine_excluded(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        aid = _park(db_conn, status="hibernating", machine="other-box")
        _add_pending_inbound(db_conn, aid)
        assert aid not in hib._select_local_swap_in_candidates(sync_pool, machine_name())


class TestSignalSwapOut:
    """The signal is gated on the pid still being that agent's own process. The
    gate exists because SIGUSR1's default disposition is *terminate*: a pid the OS
    recycled belongs to an uninvolved process, and firing at it once per restarter
    tick — which is what happened to five agents on 2026-07-30, issue #1123 —
    risks killing something that has nothing to do with Ava."""

    def test_sends_sigusr1_to_the_agents_own_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[tuple[int, int]] = []
        _stub_identity(monkeypatch, hib, AgentProcessIdentity.OWNED)
        monkeypatch.setattr(hib.os, "kill", lambda pid, sig: sent.append((pid, sig)))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        assert hib._signal_swap_out(7, _LIVE_PID) is True
        assert sent == [(_LIVE_PID, signal.SIGUSR1)]

    @pytest.mark.parametrize(
        "verdict",
        [
            AgentProcessIdentity.FOREIGN,
            AgentProcessIdentity.GONE,
            AgentProcessIdentity.UNREADABLE,
        ],
    )
    def test_never_signals_an_unverified_pid(
        self, monkeypatch: pytest.MonkeyPatch, verdict: AgentProcessIdentity
    ) -> None:
        """`OWNED` is the only verdict that authorizes the signal — no os.kill is
        even attempted for the others, so a recycled pid's new owner never gets
        touched."""
        sent: list[int] = []
        _stub_identity(monkeypatch, hib, verdict)
        monkeypatch.setattr(hib.os, "kill", lambda pid, _sig: sent.append(pid))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        assert hib._signal_swap_out(7, _DEAD_PID) is False
        assert sent == []

    def test_process_exiting_between_probe_and_signal_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe-then-kill race: the agent may exit in the gap, and that is
        nobody's fault. The corpse reaper finalizes the row."""

        def _raise(_pid, _sig):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
            raise ProcessLookupError

        _stub_identity(monkeypatch, hib, AgentProcessIdentity.OWNED)
        monkeypatch.setattr(hib.os, "kill", _raise)  # pyright: ignore[reportUnknownArgumentType]
        assert hib._signal_swap_out(7, _DEAD_PID) is False

    def test_recycled_pid_row_stops_being_signalled_every_tick(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The volley, end to end through reconcile(): the row is still selected
        (it is 'idling' past the threshold), but a FOREIGN pid draws no signal on
        this tick or any other. Before the identity gate this row produced one
        SIGUSR1 per tick indefinitely, because a stranger never converts the signal
        into the exit that would take the row out of the scan."""
        sent: list[int] = []
        _stub_identity(monkeypatch, hib, AgentProcessIdentity.FOREIGN)
        monkeypatch.setattr(hib.os, "kill", lambda pid, _sig: sent.append(pid))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        monkeypatch.setattr(settings.daemon, "hibernate_enabled", True)
        monkeypatch.setattr(settings.daemon, "hibernate_idle_threshold_seconds", 60.0)
        monkeypatch.setattr(settings.daemon, "hibernate_min_active", 0)
        aid = _park(db_conn, status="idling", pid=_LIVE_PID, idle_s_ago=120)

        controller = hib.HibernateController(sync_pool)
        for _ in range(3):
            controller.reconcile("agent-runner")

        assert sent == []
        # Still selected — clearing the row is the reaper's job, not this scan's.
        assert aid in [
            r[0] for r in hib._select_local_swap_out_candidates(sync_pool, machine_name(), 60.0, 0)
        ]


class TestControllerReconcile:
    def test_swaps_in_hibernating_with_pending(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(hib.os, "kill", lambda _pid, _sig: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        aid = _park(db_conn, status="hibernating")
        _add_pending_inbound(db_conn, aid)

        result = hib.HibernateController(sync_pool).reconcile("agent-runner")

        assert result.acted is True
        assert _status(db_conn, aid) == "idling"
        assert aid in [c.agent_id for c in launched_agents]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def test_signals_swap_out_when_enabled(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sent: list[int] = []
        _stub_identity(monkeypatch, hib, AgentProcessIdentity.OWNED)
        monkeypatch.setattr(hib.os, "kill", lambda pid, _sig: sent.append(pid))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        monkeypatch.setattr(settings.daemon, "hibernate_enabled", True)
        monkeypatch.setattr(settings.daemon, "hibernate_idle_threshold_seconds", 60.0)
        monkeypatch.setattr(settings.daemon, "hibernate_min_active", 0)
        _park(db_conn, status="idling", pid=_LIVE_PID, idle_s_ago=120)

        hib.HibernateController(sync_pool).reconcile("agent-runner")

        assert sent == [_LIVE_PID]

    def test_warm_pool_floor_exempts_recently_active_agent_from_reconcile(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end through reconcile(): a floor of 1 protects the sole
        idle-past-threshold agent from ever receiving the swap-out signal."""
        sent: list[int] = []
        _stub_identity(monkeypatch, hib, AgentProcessIdentity.OWNED)
        monkeypatch.setattr(hib.os, "kill", lambda pid, _sig: sent.append(pid))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        monkeypatch.setattr(settings.daemon, "hibernate_enabled", True)
        monkeypatch.setattr(settings.daemon, "hibernate_idle_threshold_seconds", 60.0)
        monkeypatch.setattr(settings.daemon, "hibernate_min_active", 1)
        _park(db_conn, status="idling", pid=_LIVE_PID, idle_s_ago=120)

        result = hib.HibernateController(sync_pool).reconcile("agent-runner")

        assert sent == []
        assert result.acted is False

    def test_disabled_skips_swap_out_but_still_swaps_in(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """hibernate_enabled=False stops new swap-outs but still drains already-
        hibernating agents back in (otherwise disabling would strand them)."""
        sent: list[int] = []
        _stub_identity(monkeypatch, hib, AgentProcessIdentity.OWNED)
        monkeypatch.setattr(hib.os, "kill", lambda pid, _sig: sent.append(pid))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        monkeypatch.setattr(settings.daemon, "hibernate_enabled", False)
        monkeypatch.setattr(settings.daemon, "hibernate_idle_threshold_seconds", 60.0)
        # An idle-past-threshold agent that would be swapped out if enabled …
        idle = _park(db_conn, status="idling", pid=_LIVE_PID, idle_s_ago=120)
        # … and a hibernating one with a pending inbound that must still wake.
        hibd = _park(db_conn, status="hibernating")
        _add_pending_inbound(db_conn, hibd)

        hib.HibernateController(sync_pool).reconcile("agent-runner")

        assert sent == []  # no swap-out signal while disabled
        assert _status(db_conn, idle) == "idling"  # idle agent left resident
        assert _status(db_conn, hibd) == "idling"  # hibernating one still swapped in
        assert hibd in [c.agent_id for c in launched_agents]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


# ── reaper immunity (locks the "reapers unchanged" invariant) ───────────────


class TestReaperImmunity:
    def test_running_idling_reaper_ignores_hibernating_dead_pid(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 'hibernating' row carries the dead pid of its swapped-out process. The
        running/idling corpse-reaper must NOT touch it — otherwise every swap-out
        would be reaped to 'terminated' within 30s and the mechanism destroyed."""
        _stub_identity(monkeypatch, rd, AgentProcessIdentity.GONE)
        aid = _park(db_conn, status="hibernating", pid=_DEAD_PID)

        revived = rd._revive_local_dead_running_idling(sync_pool, machine_name(), max_revive=50)

        assert aid not in revived
        assert _status(db_conn, aid) == "hibernating"

    def test_boot_phase_and_dead_birth_reapers_ignore_hibernating(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_identity(monkeypatch, rd, AgentProcessIdentity.GONE)
        aid = _park(db_conn, status="hibernating", pid=_DEAD_PID, idle_s_ago=9999)
        assert aid not in rd._reap_local_dead_boot_phase_agents(sync_pool, machine_name())
        assert aid not in rd._reap_local_unclaimed_idling(sync_pool, machine_name(), 0.0)
        assert _status(db_conn, aid) == "hibernating"


# ── heartbeat → swap-in chain (the "heartbeat wakes a hibernating agent" link) ──


class TestHeartbeatWakeChain:
    """The heartbeat wakes a hibernating agent through the same swap-in poll every
    other source uses: the daemon INSERTs a `heartbeat` inbound (no process is
    listening), and the controller's swap-in poll on the home machine sees the
    now-pending inbound and relaunches. This ties the heartbeat's raw INSERT to the
    controller's relaunch (the e2e proves the relaunch is a real process)."""

    def test_heartbeat_nudge_makes_agent_a_swap_in_candidate(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        aid = _park(db_conn, status="hibernating")
        assert aid not in hib._select_local_swap_in_candidates(sync_pool, machine_name())

        # The real heartbeat action: INSERT a 'heartbeat' inbound for the agent.
        _send_heartbeat_checkin(sync_pool, aid, idle_minutes=30.0)

        assert aid in hib._select_local_swap_in_candidates(sync_pool, machine_name())

    def test_heartbeat_nudge_then_controller_swaps_in(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End of the chain: after the heartbeat nudge, one controller reconcile
        relaunches the hibernating agent (a never-swapped idle agent would have
        woken on the same inbound), and the wake stays invisible — no lifecycle
        inbound is inserted, only the heartbeat nudge itself remains."""
        monkeypatch.setattr(hib.os, "kill", lambda _pid, _sig: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        aid = _park(db_conn, status="hibernating")
        _send_heartbeat_checkin(sync_pool, aid, idle_minutes=30.0)
        launched_agents.clear()

        result = hib.HibernateController(sync_pool).reconcile("agent-runner")

        assert result.acted is True
        assert _status(db_conn, aid) == "idling"
        assert aid in [c.agent_id for c in launched_agents]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        assert _inbound_kinds(db_conn, aid) == ["heartbeat"]  # no resurrect/restart marker added
