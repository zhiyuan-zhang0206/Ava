"""`services.heartbeat.liveness` — the gateway-owned agent-liveness pass (Task #1174).

The pass merges two signals into `agents_meta.liveness_state`: machine
reachability (status_probe against the machines-table ops URL, offline only
after 2 consecutive failed probes) and the process lease (`lease_expires_at`,
R1 #1021 — expiry with the machine up = dead process). `status` is never
touched: it stays lifecycle intent. The machine probe path is injected as a
fake so the full DB merge runs without dialing real ops servers.
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from services.heartbeat.liveness import (
    _OFFLINE_AFTER_FAILURES,
    _merge_liveness,
    run_liveness_pass,
)
from shared.config import settings
from tests.conftest import spawn_agent

_MACHINE = "test-runner-1"


@pytest.fixture
def pool():
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


def _register_machine(db: psycopg.Connection, name: str = _MACHINE) -> None:
    """Register an agent-runner machine row (probe target)."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO machines (name, gateway_url, role) VALUES (%s, %s, %s) "
            "ON CONFLICT (name) DO NOTHING",
            (name, "http://127.0.0.1:1", "{agent-runner}"),
        )
    db.commit()


def _set_machine_probe(db: psycopg.Connection, name: str, *, online: bool, failures: int) -> None:
    """Directly set a machine_probe row — the state a pass would have written."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO machine_probe (machine_name, online, consecutive_failures, last_probe_at) "
            "VALUES (%s, %s, %s, now()) "
            "ON CONFLICT (machine_name) DO UPDATE SET "
            "  online = EXCLUDED.online, "
            "  consecutive_failures = EXCLUDED.consecutive_failures, "
            "  last_probe_at = now()",
            (name, online, failures),
        )
    db.commit()


def _make_agent(
    db: psycopg.Connection,
    *,
    status: str = "idling",
    lease_s_ahead: float | None = 600.0,
    machine: str = _MACHINE,
) -> int:
    """Spawn an agent on `machine`. `lease_s_ahead` sets lease_expires_at
    relative to now() (None = NULL, negative = expired)."""
    aid = spawn_agent(spawner="user")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = %s, machine = %s, "
            "lease_expires_at = CASE WHEN %s::float IS NULL THEN NULL "
            "  ELSE now() + make_interval(secs => %s::float) END "
            "WHERE id = %s",
            (status, machine, lease_s_ahead, lease_s_ahead, aid),
        )
    db.commit()
    return aid


def _state(db: psycopg.Connection, agent_id: int) -> tuple[str, object]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT liveness_state, last_probe_at FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
        assert row is not None
        return (row[0], row[1])


class FakeProbe:
    """Injectable probe: returns per-machine reachability."""

    def __init__(self, reachable: dict[str, bool]) -> None:
        self.reachable = reachable
        self.calls: list[str] = []

    async def __call__(self, target_machine: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(target_machine)
        if not self.reachable.get(target_machine, True):
            raise ConnectionError("unreachable")
        return {"status": "completed", "result": {}}


class TestLivenessPass:
    def test_probe_timeout_comes_from_settings(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The pass's probe budget is `settings.gateway.status_probe_timeout_seconds`
        — the SAME setting the roster's probe reads, so the two probes stay
        aligned by construction — not a hardcoded 3s literal (task #1200: a
        3.0s budget flipped a slow-but-healthy WSL runner offline). A probe
        that outgrows the budget must be a failure (counted toward offline),
        never a success."""
        from shared.config import settings

        monkeypatch.setattr(settings.gateway, "status_probe_timeout_seconds", 12.0)
        _register_machine(db_conn)
        aid = _make_agent(db_conn, status="idling", lease_s_ahead=600)
        seen: dict[str, object] = {}

        class RecordingProbe(FakeProbe):
            async def __call__(self, target_machine: str, **kwargs: object) -> dict[str, object]:
                seen["timeout_s"] = kwargs.get("timeout_s")
                return await super().__call__(target_machine, **kwargs)

        import asyncio

        asyncio.run(run_liveness_pass(pool, probe=RecordingProbe({_MACHINE: True})))
        assert seen["timeout_s"] == 12.0
        assert _state(db_conn, aid)[0] == "online"

    def test_lease_expired_idling_goes_offline(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """R1 lease is the process-liveness authority: an idling row whose
        lease expired with the machine up is a dead process -> offline."""
        _register_machine(db_conn)
        aid = _make_agent(db_conn, status="idling", lease_s_ahead=-10)
        import asyncio

        asyncio.run(run_liveness_pass(pool, probe=FakeProbe({_MACHINE: True})))
        state, probed_at = _state(db_conn, aid)
        assert state == "offline"
        assert probed_at is not None

    def test_live_idling_stays_online(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        _register_machine(db_conn)
        aid = _make_agent(db_conn, status="idling", lease_s_ahead=600)
        import asyncio

        asyncio.run(run_liveness_pass(pool, probe=FakeProbe({_MACHINE: True})))
        assert _state(db_conn, aid)[0] == "online"

    def test_machine_offline_marks_every_agent_offline(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """A host judged offline (>= 2 consecutive failed probes) takes every
        non-terminated row on it offline, lease notwithstanding."""
        _register_machine(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=False, failures=_OFFLINE_AFTER_FAILURES)
        aid = _make_agent(db_conn, status="idling", lease_s_ahead=600)
        aid2 = _make_agent(db_conn, status="hibernating", lease_s_ahead=None)
        # Probe success would reset the failure count — this test exercises
        # the merge judgement directly on a pre-set probe state.
        _merge_liveness(pool)
        assert _state(db_conn, aid)[0] == "offline"
        assert _state(db_conn, aid2)[0] == "offline"

    def test_single_probe_failure_is_not_offline(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """One failed probe is a blip: consecutive_failures must reach
        _OFFLINE_AFTER_FAILURES before the machine reads offline."""
        _register_machine(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=False, failures=1)
        aid = _make_agent(db_conn, status="idling", lease_s_ahead=600)
        _merge_liveness(pool)
        assert _state(db_conn, aid)[0] == "online"

    def test_probe_failures_accumulate_and_reset(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """Consecutive failures accumulate across passes; one success resets."""
        _register_machine(db_conn)
        aid = _make_agent(db_conn, status="idling", lease_s_ahead=600)
        import asyncio

        fail = FakeProbe({_MACHINE: False})
        asyncio.run(run_liveness_pass(pool, probe=fail))
        assert _state(db_conn, aid)[0] == "online"  # 1 failure: blip
        asyncio.run(run_liveness_pass(pool, probe=fail))
        assert _state(db_conn, aid)[0] == "offline"  # 2 failures: offline
        asyncio.run(run_liveness_pass(pool, probe=FakeProbe({_MACHINE: True})))
        assert _state(db_conn, aid)[0] == "online"  # success resets

    def test_hibernating_is_lease_exempt(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """R1: hibernating holds no lease by design (swapped out) — machine up
        means online, lease expiry never applies."""
        _register_machine(db_conn)
        aid = _make_agent(db_conn, status="hibernating", lease_s_ahead=None)
        import asyncio

        asyncio.run(run_liveness_pass(pool, probe=FakeProbe({_MACHINE: True})))
        assert _state(db_conn, aid)[0] == "online"

    def test_transitional_statuses_judge_on_machine_only(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """allocated/starting/restarting hold no lease yet — machine up = online."""
        _register_machine(db_conn)
        for status in ("allocated", "starting", "restarting"):
            aid = _make_agent(db_conn, status=status, lease_s_ahead=None)
            import asyncio

            asyncio.run(run_liveness_pass(pool, probe=FakeProbe({_MACHINE: True})))
            assert _state(db_conn, aid)[0] == "online", status

    def test_terminated_rows_are_never_judged(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """A terminated row already renders dead; the pass must not touch it
        (its status is terminal by intent)."""
        _register_machine(db_conn)
        aid = _make_agent(db_conn, status="terminated", lease_s_ahead=None)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET liveness_state = 'unknown' WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        import asyncio

        asyncio.run(run_liveness_pass(pool, probe=FakeProbe({_MACHINE: True})))
        assert _state(db_conn, aid)[0] == "unknown"

    def test_unregistered_machine_stays_unknown(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """A row whose machine is not in the machines table is not judged —
        stays 'unknown' (rendered conservatively as online)."""
        aid = _make_agent(db_conn, status="idling", lease_s_ahead=-10, machine="ghost-host")
        import asyncio

        asyncio.run(run_liveness_pass(pool, probe=FakeProbe({})))
        assert _state(db_conn, aid)[0] == "unknown"

    def test_merge_is_offline_recovery_ready(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """A machine that comes back (probe success resets failures) flips its
        agents back online on the next pass — the G5 self-heal path, no
        manual status surgery."""
        _register_machine(db_conn)
        aid = _make_agent(db_conn, status="idling", lease_s_ahead=600)
        import asyncio

        fail = FakeProbe({_MACHINE: False})
        asyncio.run(run_liveness_pass(pool, probe=fail))
        asyncio.run(run_liveness_pass(pool, probe=fail))
        assert _state(db_conn, aid)[0] == "offline"
        asyncio.run(run_liveness_pass(pool, probe=FakeProbe({_MACHINE: True})))
        assert _state(db_conn, aid)[0] == "online"


class TestMachineAlertEdges:
    """Machine offline/online edges write alerts rows + IM (Task #1224).

    The liveness pass runs the shared alerts core directly (source=
    'machine-probe'); the IM fan-out is mocked at shared.alerts.notify_im.
    """

    async def _run(self, pool: ConnectionPool, probe: FakeProbe) -> None:
        await run_liveness_pass(pool, probe=probe)

    def _alerts(self, db: psycopg.Connection) -> list[tuple[object, ...]]:
        with db.cursor() as cur:
            cur.execute(
                "SELECT status, severity, alertname, source, fingerprint, notified_at "
                "FROM alerts ORDER BY starts_at"
            )
            return cur.fetchall()

    def _mock_notify(self, monkeypatch: pytest.MonkeyPatch, func: Callable[[str], bool]) -> None:
        monkeypatch.setattr("shared.alerts.notify_im", func)

    def test_offline_edge_writes_unresolved_alert_and_notifies(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _register_machine(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=True, failures=0)
        notified: list[str] = []
        self._mock_notify(monkeypatch, lambda t: notified.append(t) or True)

        import asyncio

        # failure #1: no alert yet (anti-jitter threshold)
        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        assert self._alerts(db_conn) == []
        # failure #2: the edge
        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        rows = self._alerts(db_conn)
        assert len(rows) == 1
        status, severity, alertname, source, fp, notified_at = rows[0]
        assert (status, severity, alertname, source) == (
            "unresolved",
            "error",
            "machine offline",
            "machine-probe",
        )
        assert fp  # Alertmanager fingerprint computed
        assert notified_at is not None
        assert len(notified) == 1
        firing_head = "⚠️ 告警 [ERROR] machine offline"  # emoji-ok: asserting the user-designated IM format (zh default)
        assert str(notified[0]).startswith(firing_head)

    def test_machine_offline_before_start_fires_edge_on_first_pass(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A machine whose probe row already carries a failure count past the
        threshold when this process starts (cf persisted across restarts and
        rollouts — prod 2026-08-13: machine-2 at cf=234) must still produce
        the firing edge on its first pass: the gate is `>=`, not `==`."""
        _register_machine(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=False, failures=234)
        notified: list[str] = []
        self._mock_notify(monkeypatch, lambda t: notified.append(t) or True)

        import asyncio

        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        rows = self._alerts(db_conn)
        assert len(rows) == 1
        assert rows[0][0] == "unresolved"
        assert rows[0][5] is not None
        assert len(notified) == 1

        # steady state after the edge: still one row, still one IM
        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        assert len(self._alerts(db_conn)) == 1
        assert len(notified) == 1

    def test_steady_state_failure_stays_silent(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _register_machine(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=True, failures=0)
        notified: list[str] = []
        self._mock_notify(monkeypatch, lambda t: notified.append(t) or True)

        import asyncio

        for _ in range(4):  # push the machine deep into offline steady state
            asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        rows = self._alerts(db_conn)
        assert len(rows) == 1  # one instance, no duplicates
        assert len(notified) == 1  # one IM, no storm

    def test_im_failure_retries_while_offline(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A firing IM that never landed (im_bridge down at the edge) retries
        on the next pass while the row's notified_at stays NULL."""
        _register_machine(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=True, failures=0)
        notified: list[str] = []  # successful sends only
        self._mock_notify(monkeypatch, lambda _t: False)

        import asyncio

        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        # the threshold fired on failure #2; the IM failed (edge pass)
        rows = self._alerts(db_conn)
        assert len(rows) == 1
        assert rows[0][5] is None  # notified_at still NULL

        self._mock_notify(monkeypatch, lambda t: notified.append(t) or True)
        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))  # still offline -> retry
        rows = self._alerts(db_conn)
        assert rows[0][5] is not None  # landed now
        assert len(notified) == 1

    def test_recovery_edge_resolves_and_notifies(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _register_machine(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=True, failures=0)
        notified: list[str] = []
        self._mock_notify(monkeypatch, lambda t: notified.append(t) or True)

        import asyncio

        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        assert len(notified) == 1

        asyncio.run(self._run(pool, FakeProbe({_MACHINE: True})))  # recovery
        rows = self._alerts(db_conn)
        assert len(rows) == 1
        assert rows[0][0] == "resolved"
        assert rows[0][5] is not None
        assert len(notified) == 2
        recovery_head = "✅ 已恢复 [ERROR] machine offline"  # emoji-ok: asserting the user-designated IM format (zh default)
        assert str(notified[1]).startswith(recovery_head)

        # next healthy pass is silent (no open instance to re-resolve)
        asyncio.run(self._run(pool, FakeProbe({_MACHINE: True})))
        assert len(notified) == 2

    def test_paused_machine_is_not_probed_and_fires_no_alert(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        """A PAUSED machine is dropped from `list_agent_runners()`, so the
        liveness pass neither dials it (expected absence is not an incident)
        nor writes a machine_probe row — no offline alert can fire from it.
        The paused machine's dial URL is deliberately dead (port 1); had the
        pass probed it, it would have gone offline after 2 failures."""
        _register_machine(db_conn, "away")  # dial URL 127.0.0.1:1 = dead
        _register_machine(db_conn, "still-here")  # a live member so the pass runs
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE machines SET paused_at = now(), pause_reason = 'travel' WHERE name = 'away'"
            )
        db_conn.commit()
        probe = FakeProbe({"still-here": True})

        import asyncio

        asyncio.run(run_liveness_pass(pool, probe=probe))
        # the live member is probed, the paused one is not
        assert probe.calls == ["still-here"]
        assert "away" not in probe.calls
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM machine_probe WHERE machine_name = 'away'")
            probe_row = cur.fetchone()
            assert probe_row is not None
            (n_probe_rows,) = probe_row
            cur.execute("SELECT COUNT(*) FROM alerts WHERE labels->>'machine' = 'away'")
            alert_row = cur.fetchone()
            assert alert_row is not None
            (n_alerts,) = alert_row
        assert n_probe_rows == 0
        assert n_alerts == 0
