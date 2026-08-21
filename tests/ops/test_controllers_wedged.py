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
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import ops.controllers.wedged as wedged_mod
from ops.agent_identity import AgentProcessIdentity
from ops.controllers.base import BlockScope
from shared.agents import ResurrectAlreadyAlive
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


def _park_wedged(db_conn: psycopg.Connection, *, pid: int = 1234) -> int:
    aid = spawn_agent(spawner="user")
    with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
        cur.execute(
            "UPDATE agents_meta SET status='running', pid=%s, lease_expires_at=%s WHERE id=%s",
            (pid, datetime.now(UTC) + timedelta(minutes=10), aid),
        )
    db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
    return aid


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


def _fresh_controller(pool: ConnectionPool | MagicMock) -> wedged_mod.WedgedAgentController:
    controller = wedged_mod.WedgedAgentController(cast(ConnectionPool, pool))
    controller._last_scan = 0.0  # force the first scan
    return controller


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


class TestRecovery:
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
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
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
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("SELECT status FROM agents_meta WHERE id=%s", (aid,))
            assert cur.fetchone() == ("running",)

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
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
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
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute(
                "UPDATE agents_meta SET status='terminated', termination_source='user' WHERE id=%s",
                (aid,),
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]

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
