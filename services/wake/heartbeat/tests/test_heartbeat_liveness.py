"""`services.wake.heartbeat.liveness` — the gateway-owned agent-liveness pass (Task #1174).

The pass merges two signals into `agents_meta.liveness_state`: machine
reachability (status_probe against the machines-table ops URL, offline only
after 2 consecutive failed probes) and the process lease (`lease_expires_at`,
R1 #1021 — expiry with the machine up = dead process). `status` is never
touched: it stays lifecycle intent. The machine probe path is injected as a
fake so the full DB merge runs without dialing real ops servers.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.events.contract import payload_keys
from base.events.live.bus import EventBus
from ops.cluster.rpc import ClusterOpUnreachable
from services.wake.heartbeat import JITTER_SPAN_S
from services.wake.heartbeat import daemon as heartbeat_daemon
from services.wake.heartbeat.liveness import (
    _OFFLINE_AFTER_FAILURES,
    _merge_liveness,
    run_liveness_pass,
)
from tests.fixtures.units import spawn_agent

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


def _consecutive_failures(db: psycopg.Connection, name: str) -> int:
    with db.cursor() as cur:
        cur.execute(
            "SELECT consecutive_failures FROM machine_probe WHERE machine_name = %s", (name,)
        )
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
            raise ClusterOpUnreachable("unreachable")
        return {"status": "completed", "result": {}}


class TestLivenessPass:
    def test_host_verdict_is_retained_and_cleared_on_probe_failure(
        self, db_conn: psycopg.Connection, pool: ConnectionPool
    ) -> None:
        _register_machine(db_conn)

        async def host_down(_target_machine: str = "", **_kwargs: object) -> dict[str, object]:
            return {
                "machine_name": _MACHINE,
                "serve_gateway": False,
                "serve_agent_runner": True,
                "paused": False,
                "agent_host_online": False,
            }

        import asyncio

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(), pool, EventBus.from_settings(), probe=host_down
            )
        )
        row = db_conn.execute(
            "SELECT online,agent_host_online FROM machine_probe WHERE machine_name=%s", (_MACHINE,)
        ).fetchone()
        assert row == (True, False)

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({_MACHINE: False}),
            )
        )
        row = db_conn.execute(
            "SELECT online,agent_host_online FROM machine_probe WHERE machine_name=%s", (_MACHINE,)
        ).fetchone()
        assert row == (False, None)

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
        from base.config import settings

        monkeypatch.setattr(settings.gateway, "status_probe_timeout_seconds", 12.0)
        _register_machine(db_conn)
        aid = _make_agent(db_conn, status="idling", lease_s_ahead=600)
        seen: dict[str, object] = {}

        class RecordingProbe(FakeProbe):
            async def __call__(self, target_machine: str, **kwargs: object) -> dict[str, object]:
                seen["timeout_s"] = kwargs.get("timeout_s")
                return await super().__call__(target_machine, **kwargs)

        import asyncio

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(),
                pool,
                EventBus.from_settings(),
                probe=RecordingProbe({_MACHINE: True}),
            )
        )
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

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({_MACHINE: True}),
            )
        )
        state, probed_at = _state(db_conn, aid)
        assert state == "offline"
        assert probed_at is not None

    def test_live_idling_stays_online(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        _register_machine(db_conn)
        aid = _make_agent(db_conn, status="idling", lease_s_ahead=600)
        import asyncio

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({_MACHINE: True}),
            )
        )
        assert _state(db_conn, aid)[0] == "online"

    def test_machine_offline_marks_every_agent_offline(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """A host judged offline (>= 2 consecutive failed probes) takes every
        non-terminated row on it offline, lease notwithstanding."""
        _register_machine(db_conn)
        _set_machine_probe(db_conn, _MACHINE, online=False, failures=_OFFLINE_AFTER_FAILURES)
        aid = _make_agent(db_conn, status="idling", lease_s_ahead=600)
        aid2 = _make_agent(db_conn, status="running", lease_s_ahead=None)
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
        asyncio.run(
            run_liveness_pass(Database.from_settings(), pool, EventBus.from_settings(), probe=fail)
        )
        assert _state(db_conn, aid)[0] == "online"  # 1 failure: blip
        asyncio.run(
            run_liveness_pass(Database.from_settings(), pool, EventBus.from_settings(), probe=fail)
        )
        assert _state(db_conn, aid)[0] == "offline"  # 2 failures: offline
        asyncio.run(
            run_liveness_pass(
                Database.from_settings(),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({_MACHINE: True}),
            )
        )
        assert _state(db_conn, aid)[0] == "online"  # success resets

    def test_probe_failures_count_up_and_success_resets(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        _register_machine(db_conn)
        import asyncio

        fail = FakeProbe({_MACHINE: False})
        for expected in (1, 2, 3):
            asyncio.run(
                run_liveness_pass(
                    Database.from_settings(), pool, EventBus.from_settings(), probe=fail
                )
            )
            assert _consecutive_failures(db_conn, _MACHINE) == expected

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({_MACHINE: True}),
            )
        )
        assert _consecutive_failures(db_conn, _MACHINE) == 0

    def test_pass_announces_only_liveness_edges(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A mounted frontend receives online/offline truth without a poll storm."""
        _register_machine(db_conn)
        aid = _make_agent(db_conn, status="idling")
        announced: list[int] = []

        def capture_announcement(_bus: object, agent_id: int) -> None:
            announced.append(agent_id)

        monkeypatch.setattr(
            "services.wake.heartbeat.liveness.publish_agent_updated_sync",
            capture_announcement,
        )
        import asyncio

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({_MACHINE: True}),
            )
        )
        assert announced == []  # unknown already renders as online

        announced.clear()
        asyncio.run(
            run_liveness_pass(
                Database.from_settings(),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({_MACHINE: True}),
            )
        )
        assert announced == []  # last_probe_at alone never broadcasts the fleet

        fail = FakeProbe({_MACHINE: False})
        asyncio.run(
            run_liveness_pass(Database.from_settings(), pool, EventBus.from_settings(), probe=fail)
        )
        asyncio.run(
            run_liveness_pass(Database.from_settings(), pool, EventBus.from_settings(), probe=fail)
        )
        assert announced == [aid]  # online -> offline

        announced.clear()
        asyncio.run(
            run_liveness_pass(Database.from_settings(), pool, EventBus.from_settings(), probe=fail)
        )
        assert announced == []  # offline -> offline is steady state

        announced.clear()
        asyncio.run(
            run_liveness_pass(
                Database.from_settings(),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({_MACHINE: True}),
            )
        )
        assert announced == [aid]  # offline -> online

    def test_preclaim_idling_stays_unknown(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """A freshly born idling row has no process claim, so it must not flash
        offline while the launcher is still waiting for the child to claim it."""
        _register_machine(db_conn)
        aid = _make_agent(db_conn, status="idling", lease_s_ahead=None, claimed=False)
        import asyncio

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({_MACHINE: True}),
            )
        )
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

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({_MACHINE: True}),
            )
        )
        assert _state(db_conn, aid)[0] == "unknown"

    def test_unregistered_machine_stays_unknown(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        """A row whose machine is not in the machines table is not judged —
        stays 'unknown' (rendered conservatively as online)."""
        aid = _make_agent(db_conn, status="idling", lease_s_ahead=-10, machine="ghost-host")
        import asyncio

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(), pool, EventBus.from_settings(), probe=FakeProbe({})
            )
        )
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
        asyncio.run(
            run_liveness_pass(Database.from_settings(), pool, EventBus.from_settings(), probe=fail)
        )
        asyncio.run(
            run_liveness_pass(Database.from_settings(), pool, EventBus.from_settings(), probe=fail)
        )
        assert _state(db_conn, aid)[0] == "offline"
        asyncio.run(
            run_liveness_pass(
                Database.from_settings(),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({_MACHINE: True}),
            )
        )
        assert _state(db_conn, aid)[0] == "online"


class TestMachineProbeFailedSignal:
    """Every pass in which an unpaused machine's probe fails emits one
    `machine_probe_failed` event; a reachable or paused machine emits nothing."""

    @pytest.fixture
    def emitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> list[tuple[str, str, str, dict[str, Any]]]:
        events: list[tuple[str, str, str, dict[str, Any]]] = []

        def capture(category: str, event_name: str, **kwargs: Any) -> None:
            events.append((category, event_name, kwargs["level"], kwargs["attributes"]))

        monkeypatch.setattr("base.telemetry.emit", capture)
        return events

    async def _run(self, pool: ConnectionPool, probe: FakeProbe) -> None:
        await run_liveness_pass(
            Database.from_settings(), pool, EventBus.from_settings(), probe=probe
        )

    def test_each_failed_pass_emits_with_the_running_failure_count(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        emitted: list[tuple[str, str, str, dict[str, Any]]],
    ) -> None:
        _register_machine(db_conn)

        for _ in range(3):
            asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        assert emitted == [
            ("telemetry", "machine_probe_failed", "warning", attrs)
            for attrs in (
                {"machine": _MACHINE, "consecutive_failures": 1},
                {"machine": _MACHINE, "consecutive_failures": 2},
                {"machine": _MACHINE, "consecutive_failures": 3},
            )
        ]
        # The emitted attributes are exactly the declared payload.
        assert set(emitted[0][3]) == set(payload_keys("machine_probe_failed"))

    def test_reachable_machine_emits_nothing_and_a_new_run_restarts_at_one(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        emitted: list[tuple[str, str, str, dict[str, Any]]],
    ) -> None:
        _register_machine(db_conn)

        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        asyncio.run(self._run(pool, FakeProbe({_MACHINE: True})))
        asyncio.run(self._run(pool, FakeProbe({_MACHINE: False})))
        assert [attrs["consecutive_failures"] for *_, attrs in emitted] == [1, 1]

    def test_paused_machine_is_not_probed_and_emits_nothing(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        emitted: list[tuple[str, str, str, dict[str, Any]]],
    ) -> None:
        """A PAUSED machine is dropped from `list_agent_runners()`, so the
        liveness pass neither dials it (expected absence is not an incident)
        nor writes a machine_probe row — no offline signal exists for it.
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

        asyncio.run(self._run(pool, probe))
        # the live member is probed, the paused one is not
        assert probe.calls == ["still-here"]
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM machine_probe WHERE machine_name = 'away'")
            probe_row = cur.fetchone()
            assert probe_row is not None
            (n_probe_rows,) = probe_row
        assert n_probe_rows == 0
        assert emitted == []


async def test_failed_checkin_is_retried_after_backoff_across_ticks(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    """A failed check-in stays skipped, then becomes a probe when its window ends."""
    idle_threshold = settings.daemon.heartbeat_idle_threshold_seconds
    aid = spawn_agent(spawner="user")
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'idling', "
            "last_active_at = now() - make_interval(secs => %s) WHERE id = %s",
            (idle_threshold + JITTER_SPAN_S + 100, aid),
        )
    db_conn.commit()

    start = time.time()
    tick_times = iter(
        [start, start + 1, start + 1 + idle_threshold, start + 2 + 2 * idle_threshold]
    )
    clock = [start]
    tick = [-1]
    sent_on_ticks: list[int] = []

    async def next_tick(_liveness: object, _step: float) -> None:
        try:
            clock[0] = next(tick_times)
        except StopIteration:
            raise asyncio.CancelledError from None
        tick[0] += 1

    def record_checkin(
        _pool: ConnectionPool, _db: Database, _bus: EventBus, agent_id: int, _idle_minutes: float
    ) -> None:
        if agent_id == aid:
            sent_on_ticks.append(tick[0])

    def skip_sweep(_pool: ConnectionPool) -> None:
        pass

    monkeypatch.setattr(heartbeat_daemon, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(heartbeat_daemon, "_sleep_with_liveness", next_tick)
    monkeypatch.setattr(heartbeat_daemon, "_send_heartbeat_checkin", record_checkin)
    monkeypatch.setattr(heartbeat_daemon, "_sweep_backoff_resets", skip_sweep)

    with pytest.raises(asyncio.CancelledError):
        await heartbeat_daemon._dispatch_loop(
            pool, database, event_bus, LoopProgress("dispatch", 60.0)
        )

    assert sent_on_ticks == [0, 3]
