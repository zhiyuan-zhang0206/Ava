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

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from psycopg_pool import ConnectionPool

import ops.controllers.wedged as wedged_mod
from ops.controllers.base import BlockScope
from shared.agents import ResurrectAlreadyAlive


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
def wedged_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """A wedged controller with every guard open: one candidate (id 7, pid 1234),
    gateway healthy, process alive, kill + resurrect spies."""
    claimed: list[tuple[int, int]] = [(7, 1234)]
    killed: list[int] = []
    resurrected: list[dict[str, object]] = []
    monkeypatch.setattr(
        wedged_mod,
        "_claim_wedged_candidates",
        lambda *_args, **_kwargs: list(claimed),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(wedged_mod, "_gateway_healthy", lambda: True)
    monkeypatch.setattr(wedged_mod, "process_alive", lambda pid: pid == 1234)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(wedged_mod, "force_kill", killed.append)
    monkeypatch.setattr(wedged_mod, "list_open_page_names", lambda _conn, _aid: [])  # pyright: ignore[reportUnknownArgumentType]

    def _resurrect(agent_id: int, *, resurrected_by: str, prompt: str) -> None:
        resurrected.append({"agent_id": agent_id, "by": resurrected_by, "prompt": prompt})

    monkeypatch.setattr(wedged_mod, "resurrect_agent", _resurrect)
    return {"claimed": claimed, "killed": killed, "resurrected": resurrected}


def _fresh_controller(pool: MagicMock) -> wedged_mod.WedgedAgentController:
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
    def test_dead_pid_skipped_no_kill_no_resurrect(self, wedged_env: dict[str, list[Any]]) -> None:
        """A candidate whose pid died between the claim and the kill is left for
        the reaper — no kill, no resurrect."""
        wedged_env["claimed"][:] = [(7, 9999)]  # pid 9999 != 1234 -> not alive

        controller = _fresh_controller(_fake_pool())
        result = controller.reconcile("agent-runner")

        assert wedged_env["killed"] == []
        assert wedged_env["resurrected"] == []
        assert not result.acted

    def test_kill_then_resurrect_with_prompt(self, wedged_env: dict[str, list[Any]]) -> None:
        """The recovery sequence: status CAS (terminated + 'reaper' source) ->
        force_kill -> resurrect with an explanatory prompt. Order is asserted
        because a resurrect before the kill would race a live process."""
        pool = _fake_pool(rowcount=1)
        controller = _fresh_controller(pool)

        result = controller.reconcile("agent-runner")

        assert wedged_env["killed"] == [1234]
        assert len(wedged_env["resurrected"]) == 1
        resurrect = wedged_env["resurrected"][0]
        assert resurrect["agent_id"] == 7
        assert resurrect["by"] == "system"
        assert resurrect["prompt"].startswith(
            "You were force-restarted by the wedged-agent detector"
        )
        assert result.acted is True
        # The CAS UPDATE ran (status flip to terminated before the kill).
        cur = pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        executed = [c.args[0] for c in cur.execute.call_args_list]
        assert any("termination_source = 'reaper'" in sql for sql in executed)

    def test_cas_lost_still_kills_and_resurrects(self, wedged_env: dict[str, list[Any]]) -> None:
        """A lost status CAS (row moved concurrently) does not abort the recovery —
        the kill is idempotent and the resurrect CASes the row itself."""
        pool = _fake_pool(rowcount=0)  # CAS finds 0 rows
        controller = _fresh_controller(pool)

        result = controller.reconcile("agent-runner")

        assert wedged_env["killed"] == [1234]
        assert len(wedged_env["resurrected"]) == 1
        assert result.acted is True

    def test_resurrect_already_alive_race_is_tolerated(
        self, wedged_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another recovery won the resurrect race — ResurrectAlreadyAlive is
        logged and the pass continues without crashing."""

        def _already_alive(agent_id: int, **kwargs: object) -> None:
            raise ResurrectAlreadyAlive(agent_id)

        monkeypatch.setattr(wedged_mod, "resurrect_agent", _already_alive)
        controller = _fresh_controller(_fake_pool())

        result = controller.reconcile("agent-runner")

        assert result.acted is False
        assert wedged_env["killed"] == [1234]


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
