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
from datetime import UTC, datetime
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.types.json import Jsonb
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


def _age_transition(db: psycopg.Connection, name: str, *, seconds: float) -> None:
    with db.cursor() as cur:
        cur.execute(
            "UPDATE machine_probe SET transition_since = "
            "now() - make_interval(secs => %s) WHERE machine_name = %s",
            (seconds, name),
        )
    db.commit()


def _transition_since(db: psycopg.Connection, name: str) -> datetime | None:
    with db.cursor() as cur:
        cur.execute("SELECT transition_since FROM machine_probe WHERE machine_name = %s", (name,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _make_agent(
    db: psycopg.Connection,
    *,
    status: str = "idling",
    lease_s_ahead: float | None = 600.0,
    machine: str = _MACHINE,
    claimed: bool = True,
) -> int:
    """Spawn an agent on `machine`. `lease_s_ahead` sets lease_expires_at
    relative to now() (None = NULL, negative = expired). `claimed=False`
    models a freshly created idling row whose ownership columns are all NULL."""
    aid = spawn_agent(spawner="user")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = %s, machine = %s, "
            "started_at = CASE WHEN %s THEN now() ELSE NULL END, "
            "lease_expires_at = CASE WHEN %s::float IS NULL THEN NULL "
            "  ELSE now() + make_interval(secs => %s::float) END "
            "WHERE id = %s",
            (status, machine, claimed, lease_s_ahead, lease_s_ahead, aid),
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
    def test_missing_probe_never_fabricates_online_or_observation_time(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        _register_machine(db_conn)
        aid = _make_agent(db_conn)
        _merge_liveness(pool)
        assert _state(db_conn, aid) == ("unknown", None)

    def test_merge_retains_actual_probe_time(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        _register_machine(db_conn)
        aid = _make_agent(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=True, failures=0)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE machine_probe SET last_probe_at=now()-interval '10 minutes' WHERE machine_name=%s RETURNING last_probe_at",
                (_MACHINE,),
            )
            row = cur.fetchone()
            assert row is not None
            observed = row[0]
        db_conn.commit()
        _merge_liveness(pool)
        assert _state(db_conn, aid)[1] == observed

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
        aid2 = _make_agent(db_conn, status="restarting", lease_s_ahead=None)
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

    def test_probe_failure_sets_episode_start_once_and_success_clears_it(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        _register_machine(db_conn)
        import asyncio

        fail = FakeProbe({_MACHINE: False})
        asyncio.run(run_liveness_pass(pool, probe=fail))
        started_at = _transition_since(db_conn, _MACHINE)
        assert started_at is not None

        asyncio.run(run_liveness_pass(pool, probe=fail))
        assert _transition_since(db_conn, _MACHINE) == started_at

        asyncio.run(run_liveness_pass(pool, probe=FakeProbe({_MACHINE: True})))
        assert _transition_since(db_conn, _MACHINE) is None

    def test_pass_announces_only_liveness_edges(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A mounted frontend receives online/offline truth without a poll storm."""
        _register_machine(db_conn)
        aid = _make_agent(db_conn, status="restarting", lease_s_ahead=None)
        announced: list[int] = []

        def capture_announcement(_conn: psycopg.Connection, agent_id: int) -> None:
            announced.append(agent_id)

        monkeypatch.setattr(
            "services.heartbeat.liveness.publish_agent_updated_sync",
            capture_announcement,
        )
        import asyncio

        asyncio.run(run_liveness_pass(pool, probe=FakeProbe({_MACHINE: True})))
        assert announced == []  # unknown already renders as online

        announced.clear()
        asyncio.run(run_liveness_pass(pool, probe=FakeProbe({_MACHINE: True})))
        assert announced == []  # last_probe_at alone never broadcasts the fleet

        fail = FakeProbe({_MACHINE: False})
        asyncio.run(run_liveness_pass(pool, probe=fail))
        asyncio.run(run_liveness_pass(pool, probe=fail))
        assert announced == [aid]  # online -> offline

        announced.clear()
        asyncio.run(run_liveness_pass(pool, probe=fail))
        assert announced == []  # offline -> offline is steady state

        announced.clear()
        asyncio.run(run_liveness_pass(pool, probe=FakeProbe({_MACHINE: True})))
        assert announced == [aid]  # offline -> online

    def test_preclaim_idling_stays_unknown(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """A freshly born idling row has no process claim, so it must not flash
        offline while the launcher is still waiting for the child to claim it."""
        _register_machine(db_conn)
        aid = _make_agent(db_conn, status="idling", lease_s_ahead=None, claimed=False)
        import asyncio

        asyncio.run(run_liveness_pass(pool, probe=FakeProbe({_MACHINE: True})))
        assert _state(db_conn, aid)[0] == "unknown"

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

    def test_recent_failure_tracks_episode_without_alerting(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _register_machine(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=True, failures=0)
        notified: list[str] = []
        self._mock_notify(monkeypatch, lambda text: notified.append(text) or True)

        import asyncio

        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        assert self._alerts(db_conn) == []
        assert _transition_since(db_conn, _MACHINE) is not None
        assert notified == []

    def test_warning_escalates_to_error_on_one_instance(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings.alerts, "transition_warning_seconds", 60.0)
        monkeypatch.setattr(settings.alerts, "transition_error_seconds", 120.0)
        _register_machine(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=False, failures=2)
        _age_transition(db_conn, _MACHINE, seconds=61)
        notified: list[str] = []
        self._mock_notify(monkeypatch, lambda text: notified.append(text) or True)

        import asyncio

        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        rows = self._alerts(db_conn)
        assert len(rows) == 1
        assert rows[0][1] == "warning"
        warning_fp = rows[0][4]
        assert rows[0][5] is not None
        assert len(notified) == 1

        _age_transition(db_conn, _MACHINE, seconds=121)
        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        rows = self._alerts(db_conn)
        assert len(rows) == 1
        assert rows[0][1] == "error"
        assert rows[0][4] == warning_fp
        assert len(notified) == 2

        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        assert len(self._alerts(db_conn)) == 1
        assert len(notified) == 2

    def test_im_failure_retries_while_offline(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _register_machine(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=False, failures=2)
        _age_transition(db_conn, _MACHINE, seconds=181)
        notified: list[str] = []
        self._mock_notify(monkeypatch, lambda _text: False)

        import asyncio

        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        rows = self._alerts(db_conn)
        assert len(rows) == 1
        assert rows[0][5] is None

        self._mock_notify(monkeypatch, lambda text: notified.append(text) or True)
        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        assert self._alerts(db_conn)[0][5] is not None
        assert len(notified) == 1

    def test_recovery_edge_resolves_and_notifies(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _register_machine(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=False, failures=2)
        _age_transition(db_conn, _MACHINE, seconds=181)
        notified: list[str] = []
        self._mock_notify(monkeypatch, lambda text: notified.append(text) or True)

        import asyncio

        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        assert len(notified) == 1

        asyncio.run(self._run(pool, FakeProbe({_MACHINE: True})))
        rows = self._alerts(db_conn)
        assert len(rows) == 1
        assert rows[0][0] == "resolved"
        assert len(notified) == 2

        asyncio.run(self._run(pool, FakeProbe({_MACHINE: True})))
        assert len(notified) == 2

    def test_cluster_deploy_explains_then_grades_from_true_start(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _register_machine(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=False, failures=2)
        _age_transition(db_conn, _MACHINE, seconds=601)
        monkeypatch.setattr("shared.cluster_lock.read_update_lease", object)
        notified: list[str] = []
        self._mock_notify(monkeypatch, lambda text: notified.append(text) or True)

        import asyncio

        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        assert self._alerts(db_conn) == []

        monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: None)
        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        assert self._alerts(db_conn)[0][1] == "error"
        assert len(notified) == 1

    def test_host_updater_lease_explains_transition(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _register_machine(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=False, failures=2)
        _age_transition(db_conn, _MACHINE, seconds=601)
        monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: None)
        monkeypatch.setattr(
            "shared.host_deploy_state.read_all",
            lambda: {_MACHINE: SimpleNamespace(updater_live=True)},
        )

        import asyncio

        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        assert self._alerts(db_conn) == []

    def test_unreadable_deploy_context_fails_open(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _register_machine(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=False, failures=2)
        _age_transition(db_conn, _MACHINE, seconds=601)
        monkeypatch.setattr(
            "shared.cluster_lock.read_update_lease",
            lambda: (_ for _ in ()).throw(RuntimeError("unreadable")),
        )

        import asyncio

        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        assert self._alerts(db_conn)[0][1] == "error"

    def test_recovery_resolves_preconvention_and_stable_fingerprint_rows(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from shared.alerts import fingerprint

        _register_machine(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=False, failures=3)
        _age_transition(db_conn, _MACHINE, seconds=181)
        identity_labels = {"alertname": "machine offline", "machine": _MACHINE}
        old_labels = {**identity_labels, "severity": "warning"}
        stable_labels = {**identity_labels, "severity": "error"}
        starts_at = datetime(2026, 8, 26, tzinfo=UTC)
        old_fingerprint = fingerprint(old_labels)
        stable_fingerprint = fingerprint(identity_labels)
        with db_conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO alerts (status, severity, alertname, labels, annotations, "
                "starts_at, fingerprint, source, notified_at) VALUES "
                "('unresolved', %s, 'machine offline', %s, '{}', %s, %s, "
                "'machine-probe', now())",
                [
                    ("warning", Jsonb(old_labels), starts_at, old_fingerprint),
                    ("error", Jsonb(stable_labels), starts_at, stable_fingerprint),
                ],
            )
        db_conn.commit()
        notified: list[str] = []
        self._mock_notify(monkeypatch, lambda text: notified.append(text) or True)

        import asyncio

        asyncio.run(self._run(pool, FakeProbe({_MACHINE: True})))
        rows = self._alerts(db_conn)
        assert {(row[0], row[1], row[4]) for row in rows} == {
            ("resolved", "warning", old_fingerprint),
            ("resolved", "error", stable_fingerprint),
        }
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
