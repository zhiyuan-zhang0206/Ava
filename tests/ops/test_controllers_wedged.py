"""`ops.controllers.wedged` — the live-but-stuck agent detector.

Zero coverage before the 2026-08-08 audit (P2-1): this controller is the ONLY
component that SIGKILLs a live process and then manually resurrects it, so its
guards (role gate, enable gate, scan throttle, gateway-health gate, dead-pid
skip, per-agent backoff via the claim SQL) and its kill→resurrect ordering are
locked here. The candidate-claim SQL itself (atomic UPDATE ... RETURNING with
the `lease_expires_at > now()` gate) is asserted textually — it is the
controller's race-safe single-claim mechanism, and a mutation that drops the
lease gate would let the wedged pass kill paused-but-alive agents whose lease
expired during a DB outage (the same pause the lease-zombie grace protects).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import ops.controllers.wedged as wedged_mod
from ops import ops_lifecycle
from ops.agent_identity import AgentProcessIdentity
from ops.controllers.base import BlockScope
from ops.rpc_schemas import RestartAgentRequest
from shared.agents import AgentStatus, ResurrectAlreadyAlive
from shared.config import settings
from tests.conftest import spawn_agent


def _fake_pool(*, rowcount: int = 1) -> MagicMock:
    """A pool whose `connection()` yields a conn whose cursor reports rowcount —
    the shape wedged's kill branch uses for its status CAS."""
    cur = MagicMock(name="cur")
    cur.rowcount = rowcount
    conn = MagicMock(name="conn")
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    pool = MagicMock(name="pool")
    pool.connection.return_value.__enter__.return_value = conn
    pool.connection.return_value.__exit__.return_value = False
    return pool


@pytest.fixture
def sync_pool() -> Iterator[ConnectionPool]:
    pool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=4, open=True)
    try:
        yield cast(ConnectionPool, pool)
    finally:
        pool.close()


def _park_wedged(db_conn: psycopg.Connection, *, pid: int = 1234, status: str = "running") -> int:
    aid = spawn_agent(spawner="user")
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status=%s, pid=%s, lease_expires_at=%s WHERE id=%s",
            (status, pid, datetime.now(UTC) + timedelta(minutes=10), aid),
        )
    db_conn.commit()
    return aid


def _add_stale_pending_chat(db_conn: psycopg.Connection, aid: int, *, age_s: float) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, created_at) "
            "VALUES (%s, 'stale work', 'chat', 'user', "
            "now() - make_interval(secs => %s))",
            (aid, age_s),
        )
    db_conn.commit()


def _set_turn_timestamps(
    db_conn: psycopg.Connection,
    aid: int,
    *,
    status_age_s: float,
    last_active_age_s: float,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status_changed_at = now() - make_interval(secs => %s), "
            "last_active_at = now() - make_interval(secs => %s) WHERE id = %s",
            (status_age_s, last_active_age_s, aid),
        )
    db_conn.commit()


def _add_pending_lifecycle_inbound(db_conn: psycopg.Connection, aid: int, *, kind: str) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, '', %s, 'user')",
            (aid, kind),
        )
    db_conn.commit()


def _add_pending_resurrect_inbound(db_conn: psycopg.Connection, aid: int) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, '', 'resurrect', 'system')",
            (aid,),
        )
    db_conn.commit()


def _owned_process(_pid: int, _agent_id: int) -> AgentProcessIdentity:
    return AgentProcessIdentity.OWNED


def _open_recovery(
    monkeypatch: pytest.MonkeyPatch,
    aid: int,
    *,
    pid: int,
    identities: list[AgentProcessIdentity],
) -> tuple[list[int], list[dict[str, object]]]:
    claimed_at = datetime.now(UTC)
    monkeypatch.setattr(
        wedged_mod,
        "_claim_wedged_candidates",
        lambda *_args, **_kwargs: [(aid, pid, claimed_at)],  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(wedged_mod, "_gateway_healthy", lambda: True)
    verdicts = iter(identities)
    monkeypatch.setattr(wedged_mod, "probe_agent_process", lambda *_a: next(verdicts))  # pyright: ignore[reportUnknownArgumentType]
    killed: list[int] = []
    resurrected: list[dict[str, object]] = []
    monkeypatch.setattr(wedged_mod, "force_kill", killed.append)

    def _resurrect(agent_id: int, **kwargs: object) -> None:
        resurrected.append({"agent_id": agent_id, **kwargs})

    monkeypatch.setattr(wedged_mod, "resurrect_agent", _resurrect)
    return killed, resurrected


def _capture_recovery_with_real_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[int], list[int]]:
    """Capture recovery side effects without replacing the candidate claim SQL."""
    monkeypatch.setattr(settings.daemon, "wedged_agent_enabled", True)
    monkeypatch.setattr(settings.daemon, "wedged_agent_inbound_age_seconds", 60.0)
    monkeypatch.setattr(wedged_mod, "_gateway_healthy", lambda: True)

    monkeypatch.setattr(wedged_mod, "probe_agent_process", _owned_process)
    killed: list[int] = []
    resurrected: list[int] = []
    monkeypatch.setattr(wedged_mod, "force_kill", killed.append)

    def _capture_resurrect(agent_id: int, **_kwargs: object) -> None:
        resurrected.append(agent_id)

    monkeypatch.setattr(wedged_mod, "resurrect_agent", _capture_resurrect)
    return killed, resurrected


def _fresh_controller(pool: ConnectionPool | MagicMock) -> wedged_mod.WedgedAgentController:
    controller = wedged_mod.WedgedAgentController(cast(ConnectionPool, pool))
    controller._last_scan = 0.0  # force the first scan
    return controller


@pytest.fixture(autouse=True)
def _host_is_serving(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Existing controller cases model a host that passed its start gate."""
    from shared import start_serving

    monkeypatch.setattr(start_serving, "state_path", lambda: tmp_path / "start-serving.json")
    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation) is True


class TestGuards:
    def test_role_gate_skips_non_agent_runner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            wedged_mod,
            "_claim_wedged_candidates",
            lambda *_args, **_kwargs: pytest.fail("must not scan"),  # pyright: ignore[reportUnknownArgumentType]
        )
        controller = _fresh_controller(MagicMock())
        result = controller.reconcile("gateway")
        assert result.blocks is BlockScope.NONE and not result.acted

    def test_disabled_setting_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "shared.config.settings.daemon.wedged_agent_enabled", False, raising=False
        )
        monkeypatch.setattr(
            wedged_mod,
            "_claim_wedged_candidates",
            lambda *_args, **_kwargs: pytest.fail("must not scan"),  # pyright: ignore[reportUnknownArgumentType]
        )
        controller = _fresh_controller(MagicMock())
        result = controller.reconcile("agent-runner")
        assert not result.acted

    def test_scan_throttle_skips_within_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            wedged_mod,
            "_claim_wedged_candidates",
            lambda *_args, **_kwargs: pytest.fail("must not scan"),  # pyright: ignore[reportUnknownArgumentType]
        )
        controller = _fresh_controller(MagicMock())
        controller.reconcile("agent-runner")  # first scan arms the throttle
        result = controller.reconcile("agent-runner")  # second is throttled
        assert not result.acted

    def test_gateway_unhealthy_defers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wedged_mod, "_gateway_healthy", lambda: False)
        monkeypatch.setattr(
            wedged_mod,
            "_claim_wedged_candidates",
            lambda *_args, **_kwargs: pytest.fail("must not scan"),  # pyright: ignore[reportUnknownArgumentType]
        )
        controller = _fresh_controller(MagicMock())
        result = controller.reconcile("agent-runner")
        assert not result.acted

    def test_not_serving_defers_recovery_but_keeps_zombie_cleanup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed start may reap terminated zombies but cannot relaunch work."""
        from shared import start_serving

        monkeypatch.setattr(settings.daemon, "wedged_agent_enabled", True)
        start_serving.clear_serving()
        reaped: list[int] = []

        def _must_not_claim(
            _pool: ConnectionPool,
            _local_machine: str,
            _running_age_s: float,
            _idling_age_s: float,
            _backoff_s: float,
        ) -> list[tuple[int, int, datetime]]:
            pytest.fail("must not claim a recovery before serving")

        def _terminated_zombies(
            _pool: ConnectionPool, _local_machine: str, _backoff_s: float
        ) -> list[tuple[int, int]]:
            return [(7, 1234)]

        def _reap_terminated_zombie(_pool: ConnectionPool, agent_id: int, _pid: int) -> bool:
            reaped.append(agent_id)
            return True

        monkeypatch.setattr(wedged_mod, "_gateway_healthy", lambda: True)
        monkeypatch.setattr(wedged_mod, "_claim_wedged_candidates", _must_not_claim)
        monkeypatch.setattr(wedged_mod, "_claim_terminated_lease_zombies", _terminated_zombies)
        monkeypatch.setattr(wedged_mod, "_reap_terminated_lease_zombie", _reap_terminated_zombie)

        result = _fresh_controller(MagicMock()).reconcile("agent-runner")

        assert result.acted is True
        assert reaped == [7]


class TestRecovery:
    def test_running_stale_turn_without_pending_is_recovered(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        killed, resurrected = _capture_recovery_with_real_claim(monkeypatch)
        aid = _park_wedged(db_conn)
        _set_turn_timestamps(db_conn, aid, status_age_s=120.0, last_active_age_s=120.0)

        result = _fresh_controller(sync_pool).reconcile("agent-runner")

        assert result.acted is True
        assert killed == [1234]
        assert resurrected == [aid]

    def test_running_stale_turn_with_recent_round_is_not_reaped(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        killed, resurrected = _capture_recovery_with_real_claim(monkeypatch)
        aid = _park_wedged(db_conn)
        _set_turn_timestamps(db_conn, aid, status_age_s=120.0, last_active_age_s=30.0)

        result = _fresh_controller(sync_pool).reconcile("agent-runner")

        assert result.acted is False
        assert killed == []
        assert resurrected == []

    def test_running_fresh_turn_is_not_reaped(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        killed, resurrected = _capture_recovery_with_real_claim(monkeypatch)
        aid = _park_wedged(db_conn)
        _set_turn_timestamps(db_conn, aid, status_age_s=30.0, last_active_age_s=120.0)

        result = _fresh_controller(sync_pool).reconcile("agent-runner")

        assert result.acted is False
        assert killed == []
        assert resurrected == []

    def test_idling_stale_claim_loop_is_recovered_without_pending_inbound(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A live lease only says the renewer runs. A stale idle claim marker
        means no fallback SELECT is advancing and warrants OOB recovery even
        before the next inbound arrives."""
        monkeypatch.setattr(settings.daemon, "wedged_agent_inbound_age_seconds", 2400.0)
        monkeypatch.setattr(settings.daemon, "wedged_idling_agent_inbound_age_seconds", 60.0)
        killed, resurrected = _capture_recovery_with_real_claim(monkeypatch)
        aid = _park_wedged(db_conn, status="idling")
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET last_claim_loop_at = now() - interval '120 seconds', "
                "status_changed_at = now() - interval '120 seconds' "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()

        result = _fresh_controller(sync_pool).reconcile("agent-runner")

        assert result.acted is True
        assert killed == [1234]
        assert resurrected == [aid]

    @pytest.mark.asyncio
    async def test_idling_pending_restart_is_recovered_out_of_band(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A restart waits in the same inbound queue and needs the same OOB recovery."""
        monkeypatch.setattr(settings.daemon, "wedged_agent_inbound_age_seconds", 2400.0)
        monkeypatch.setattr(settings.daemon, "wedged_idling_agent_inbound_age_seconds", 60.0)

        def _idling_status(_agent_id: int) -> AgentStatus:
            return AgentStatus.IDLING

        monkeypatch.setattr(ops_lifecycle, "get_agent_status", _idling_status)
        published: list[int] = []

        async def _published(agent_id: int, inbound_id: int, *_args: object) -> None:
            assert agent_id > 0
            published.append(inbound_id)

        monkeypatch.setattr(ops_lifecycle, "publish_inbound_arrived", _published)
        monkeypatch.setattr(wedged_mod, "_gateway_healthy", lambda: True)
        monkeypatch.setattr(wedged_mod, "probe_agent_process", _owned_process)
        killed: list[int] = []
        resurrected: list[int] = []
        monkeypatch.setattr(wedged_mod, "force_kill", killed.append)

        def _capture_resurrect(agent_id: int, **_kwargs: object) -> None:
            resurrected.append(agent_id)

        monkeypatch.setattr(wedged_mod, "resurrect_agent", _capture_resurrect)
        aid = _park_wedged(db_conn, status="idling")

        response = await ops_lifecycle.restart_agent_op(
            aid, RestartAgentRequest(source="user"), sync_pool
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages SET created_at = now() - interval '120 seconds' "
                "WHERE agent_id = %s AND kind = 'restart'",
                (aid,),
            )
            cur.execute(
                "UPDATE agents_meta SET status_changed_at = now() - interval '120 seconds' "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()

        result = _fresh_controller(sync_pool).reconcile("agent-runner")

        assert response.status == "enqueued"
        assert len(published) == 1
        assert result.acted is True
        assert killed == [1234]
        assert resurrected == [aid]

    def test_terminated_agent_with_live_lease_is_reaped_without_resurrection(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A user-terminated process retaining its lease must not survive as a zombie."""
        monkeypatch.setattr(wedged_mod, "_gateway_healthy", lambda: True)
        monkeypatch.setattr(wedged_mod, "probe_agent_process", _owned_process)
        killed: list[int] = []
        resurrected: list[int] = []
        monkeypatch.setattr(wedged_mod, "force_kill", killed.append)

        def _capture_resurrect(agent_id: int, **_kwargs: object) -> None:
            resurrected.append(agent_id)

        monkeypatch.setattr(wedged_mod, "resurrect_agent", _capture_resurrect)
        aid = _park_wedged(db_conn, status="terminated")
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET termination_source='user' WHERE id=%s", (aid,))
        db_conn.commit()
        _add_pending_lifecycle_inbound(db_conn, aid, kind="terminate")

        result = _fresh_controller(sync_pool).reconcile("agent-runner")

        assert result.acted is True
        assert killed == [1234]
        assert resurrected == []
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status, pid, lease_expires_at, termination_source "
                "FROM agents_meta WHERE id=%s",
                (aid,),
            )
            assert cur.fetchone() == ("terminated", None, None, "user")

    def test_terminated_zombie_reap_does_not_require_gateway_health(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reaping a user-terminated zombie must not wait for a future resurrect."""
        monkeypatch.setattr(wedged_mod, "_gateway_healthy", lambda: False)
        monkeypatch.setattr(wedged_mod, "probe_agent_process", _owned_process)
        killed: list[int] = []
        monkeypatch.setattr(wedged_mod, "force_kill", killed.append)
        aid = _park_wedged(db_conn, status="terminated")
        _add_pending_lifecycle_inbound(db_conn, aid, kind="terminate")

        result = _fresh_controller(sync_pool).reconcile("agent-runner")

        assert result.acted is True
        assert killed == [1234]

    def test_idling_candidate_uses_short_threshold(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An idling agent cannot legitimately leave work pending for a turn budget.

        Regression: the running-turn threshold let an idling process retain a
        pending restart or chat for 40 minutes despite a live lease.
        """
        monkeypatch.setattr(settings.daemon, "wedged_agent_inbound_age_seconds", 2400.0)
        monkeypatch.setattr(settings.daemon, "wedged_idling_agent_inbound_age_seconds", 60.0)
        monkeypatch.setattr(wedged_mod, "_gateway_healthy", lambda: True)
        monkeypatch.setattr(wedged_mod, "probe_agent_process", _owned_process)
        killed: list[int] = []
        resurrected: list[int] = []
        monkeypatch.setattr(wedged_mod, "force_kill", killed.append)

        def _capture_resurrect(agent_id: int, **_kwargs: object) -> None:
            resurrected.append(agent_id)

        monkeypatch.setattr(wedged_mod, "resurrect_agent", _capture_resurrect)
        aid = _park_wedged(db_conn, status="idling")
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status_changed_at = now() - interval '120 seconds' "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        _add_stale_pending_chat(db_conn, aid, age_s=120.0)

        result = _fresh_controller(sync_pool).reconcile("agent-runner")

        assert result.acted is True
        assert killed == [1234]
        assert resurrected == [aid]

    def test_idling_pending_older_than_threshold_is_not_reaped_just_after_turn(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A long legitimate turn can finish with an old pending inbound. Its
        idling status is new, so the normal first claim must win the short race
        instead of the wedged controller killing a healthy process."""
        monkeypatch.setattr(settings.daemon, "wedged_agent_inbound_age_seconds", 2400.0)
        monkeypatch.setattr(settings.daemon, "wedged_idling_agent_inbound_age_seconds", 60.0)
        killed, resurrected = _capture_recovery_with_real_claim(monkeypatch)
        aid = _park_wedged(db_conn, status="idling")
        _add_stale_pending_chat(db_conn, aid, age_s=120.0)

        result = _fresh_controller(sync_pool).reconcile("agent-runner")

        assert result.acted is False
        assert killed == []
        assert resurrected == []
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (aid,))
            assert cur.fetchone() == ("idling",)

    def test_idling_pending_terminate_is_reaped_without_resurrection(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A terminal intent queued behind a dead idle loop must end that
        process, never convert it into an automatic resurrection."""
        aid = _park_wedged(db_conn, status="idling")
        _add_pending_lifecycle_inbound(db_conn, aid, kind="terminate")
        killed, resurrected = _open_recovery(
            monkeypatch,
            aid,
            pid=1234,
            identities=[AgentProcessIdentity.OWNED, AgentProcessIdentity.OWNED],
        )

        result = _fresh_controller(sync_pool).reconcile("agent-runner")

        assert result.acted is True
        assert killed == [1234]
        assert resurrected == []
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (aid,))
            assert cur.fetchone() == ("terminated",)

    def test_attempt_budget_stops_wedged_recovery_at_limit(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stale chat cannot feed the wedged kill/resurrect loop after three
        earlier recovery boots all failed before consuming their lifecycle row."""
        killed, resurrected = _capture_recovery_with_real_claim(monkeypatch)
        aid = _park_wedged(db_conn)
        _add_stale_pending_chat(db_conn, aid, age_s=120.0)
        for _ in range(3):
            _add_pending_resurrect_inbound(db_conn, aid)

        result = _fresh_controller(sync_pool).reconcile("agent-runner")

        assert killed == []
        assert resurrected == []
        assert result.acted is False
        with db_conn.cursor() as cur:
            cur.execute("SELECT status, termination_source FROM agents_meta WHERE id=%s", (aid,))
            assert cur.fetchone() == ("running", None)

    def test_attempt_budget_allows_wedged_recovery_with_no_failed_attempts(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stale-chat candidate with no failed recovery lifecycle rows passes
        the real claim query and completes the wedged recovery path."""
        killed, resurrected = _capture_recovery_with_real_claim(monkeypatch)
        aid = _park_wedged(db_conn)
        _add_stale_pending_chat(db_conn, aid, age_s=120.0)

        result = _fresh_controller(sync_pool).reconcile("agent-runner")

        assert killed == [1234]
        assert resurrected == [aid]
        assert result.acted is True

    @pytest.mark.parametrize(
        ("identity", "expected_kill"),
        [
            (AgentProcessIdentity.OWNED, [1234]),
            (AgentProcessIdentity.FOREIGN, []),
            (AgentProcessIdentity.GONE, []),
        ],
    )
    def test_identity_evidence_reconciles_without_signalling_foreign_pid(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        identity: AgentProcessIdentity,
        expected_kill: list[int],
    ) -> None:
        aid = _park_wedged(db_conn)
        killed, resurrected = _open_recovery(
            monkeypatch, aid, pid=1234, identities=[identity, identity]
        )

        result = _fresh_controller(sync_pool).reconcile("agent-runner")

        assert killed == expected_kill
        assert len(resurrected) == 1
        assert resurrected[0]["agent_id"] == aid
        assert str(resurrected[0]["prompt"]).startswith(
            "You were restarted by the wedged-agent detector"
        )
        with db_conn.cursor() as cur:
            cur.execute("SELECT status, termination_source FROM agents_meta WHERE id=%s", (aid,))
            assert cur.fetchone() == ("terminated", "reaper")
        assert result.acted is True

    def test_unreadable_identity_defers_without_transition(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        aid = _park_wedged(db_conn)
        killed, resurrected = _open_recovery(
            monkeypatch, aid, pid=1234, identities=[AgentProcessIdentity.UNREADABLE]
        )

        result = _fresh_controller(sync_pool).reconcile("agent-runner")

        assert killed == [] and resurrected == [] and not result.acted
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id=%s", (aid,))
            assert cur.fetchone() == ("running",)
        assert result.detail is None

    def test_identity_becoming_unreadable_under_lock_defers(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        aid = _park_wedged(db_conn)
        killed, resurrected = _open_recovery(
            monkeypatch,
            aid,
            pid=1234,
            identities=[AgentProcessIdentity.OWNED, AgentProcessIdentity.UNREADABLE],
        )

        result = _fresh_controller(sync_pool).reconcile("agent-runner")

        assert killed == [] and resurrected == [] and not result.acted
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id=%s", (aid,))
            assert cur.fetchone() == ("running",)

    def test_user_force_before_transition_skips_kill_and_resurrect(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        aid = _park_wedged(db_conn)
        killed, resurrected = _open_recovery(
            monkeypatch,
            aid,
            pid=1234,
            identities=[AgentProcessIdentity.OWNED, AgentProcessIdentity.OWNED],
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status='terminated', termination_source='user' WHERE id=%s",
                (aid,),
            )
        db_conn.commit()

        result = _fresh_controller(sync_pool).reconcile("agent-runner")

        assert killed == [] and resurrected == [] and not result.acted

    def test_resurrect_already_alive_race_is_tolerated(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Another recovery won the resurrect race — ResurrectAlreadyAlive is
        logged and the pass continues without crashing."""

        def _already_alive(agent_id: int, **kwargs: object) -> None:
            raise ResurrectAlreadyAlive(agent_id)

        aid = _park_wedged(db_conn)
        killed, _ = _open_recovery(
            monkeypatch,
            aid,
            pid=1234,
            identities=[AgentProcessIdentity.OWNED, AgentProcessIdentity.OWNED],
        )
        monkeypatch.setattr(wedged_mod, "resurrect_agent", _already_alive)
        controller = _fresh_controller(sync_pool)

        result = controller.reconcile("agent-runner")

        assert result.acted is False
        assert killed == [1234]


class TestClaimSQL:
    def test_claim_sql_is_atomic_and_lease_gated(self) -> None:
        """The candidate claim is one UPDATE ... RETURNING (atomic single-claim)
        and gates on `lease_expires_at > now()` — a wedged pass must never pick
        a paused-but-alive agent whose lease expired during a DB outage."""
        import inspect

        src = inspect.getsource(wedged_mod._claim_wedged_candidates)
        assert "UPDATE agents_meta" in src
        assert "RETURNING id, pid" in src
        assert "lease_expires_at > now()" in src
        assert "status IN ('running', 'idling')" in src
        # The backoff stamp and the claim happen in the SAME statement — a
        # concurrent pass cannot double-claim the same agent.
        assert "last_wedged_check_at = now()" in src
        assert "now() - last_wedged_check_at" in src
        assert "lc.kind = 'resurrect'" in src
        assert "lc.status = 'pending'" in src
        assert "agents_meta.status_changed_at" in src
        assert "agents_meta.last_active_at" in src
        assert "COALESCE(" in src


class TestTerminatedZombieEvidence:
    @pytest.mark.parametrize(
        ("identity", "expected_kill"),
        [
            (AgentProcessIdentity.FOREIGN, []),
            (AgentProcessIdentity.GONE, []),
        ],
    )
    def test_foreign_or_gone_terminated_zombie_is_cleared_without_signal(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        identity: AgentProcessIdentity,
        expected_kill: list[int],
    ) -> None:
        """A stale row may name a reused pid. FOREIGN and GONE are sufficient
        evidence to clear the zombie projection, but never to signal it."""
        aid = _park_wedged(db_conn, status="terminated")
        killed: list[int] = []
        monkeypatch.setattr(wedged_mod, "force_kill", killed.append)

        def _identity(*_args: object) -> AgentProcessIdentity:
            return identity

        monkeypatch.setattr(wedged_mod, "probe_agent_process", _identity)

        assert wedged_mod._reap_terminated_lease_zombie(sync_pool, aid, 1234) is True
        assert killed == expected_kill
        with db_conn.cursor() as cur:
            cur.execute("SELECT pid, lease_expires_at FROM agents_meta WHERE id = %s", (aid,))
            assert cur.fetchone() == (None, None)

    def test_unreadable_terminated_zombie_is_deferred(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No process evidence means preserve the row for the next scan rather
        than clearing a possibly live process's lease or PID."""
        pool = _fake_pool()

        def _unreadable(*_args: object) -> AgentProcessIdentity:
            return AgentProcessIdentity.UNREADABLE

        monkeypatch.setattr(wedged_mod, "probe_agent_process", _unreadable)

        assert wedged_mod._reap_terminated_lease_zombie(pool, 7, 1234) is False
        pool.connection.assert_not_called()

    def test_concurrent_status_change_that_loses_clear_cas_is_deferred(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The zombie reaper's status/PID/lease CAS can lose to a concurrent
        lifecycle write; no force-kill or false success may follow rowcount=0."""
        pool = _fake_pool(rowcount=0)
        cur = pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("terminated", 1234, True)
        killed: list[int] = []
        monkeypatch.setattr(wedged_mod, "force_kill", killed.append)
        monkeypatch.setattr(wedged_mod, "probe_agent_process", _owned_process)

        assert wedged_mod._reap_terminated_lease_zombie(pool, 7, 1234) is False
        assert killed == []
