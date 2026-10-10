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

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from base.config.service_read import ConfigAuthority
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.wake.heartbeat import JITTER_SPAN_S
from services.wake.heartbeat import daemon as heartbeat_daemon
from services.wake.heartbeat.liveness import (
    _OFFLINE_AFTER_FAILURES,
    _merge_liveness,
    run_liveness_pass,
)
from services.wake.heartbeat.tests.machine_probe_setup import (
    MACHINE,
    FakeProbe,
    pool,
    register_machine,
)
from tests.fixtures.units import spawn_agent


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
    machine: str = MACHINE,
    claimed: bool = True,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> int:
    """Spawn an agent on `machine`. `lease_s_ahead` sets lease_expires_at
    relative to now() (None = NULL, negative = expired). `claimed=False`
    models a freshly created idling row whose ownership columns are all NULL."""
    aid = spawn_agent(spawner="user", catalog=model_catalog, authority=config_authority)
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


class TestLivenessPass:
    @pytest.mark.usefixtures(pool.__name__)
    def test_host_verdict_is_retained_and_cleared_on_probe_failure(
        self, db_conn: psycopg.Connection, pool: ConnectionPool, *, database_gate: ProcessDbGate
    ) -> None:
        register_machine(db_conn)

        async def host_down(_target_machine: str = "", **_kwargs: object) -> dict[str, object]:
            return {
                "machine_name": MACHINE,
                "serve_gateway": False,
                "serve_agent_runner": True,
                "paused": False,
                "agent_host_online": False,
            }

        import asyncio

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=host_down,
            )
        )
        row = db_conn.execute(
            "SELECT online,agent_host_online FROM machine_probe WHERE machine_name=%s", (MACHINE,)
        ).fetchone()
        assert row == (True, False)

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({MACHINE: False}),
            )
        )
        row = db_conn.execute(
            "SELECT online,agent_host_online FROM machine_probe WHERE machine_name=%s", (MACHINE,)
        ).fetchone()
        assert row == (False, None)

    @pytest.mark.usefixtures(pool.__name__)
    def test_missing_probe_never_fabricates_online_or_observation_time(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        register_machine(db_conn)
        aid = _make_agent(db_conn, model_catalog=model_catalog, config_authority=config_authority)
        _merge_liveness(pool)
        assert _state(db_conn, aid) == ("unknown", None)

    @pytest.mark.usefixtures(pool.__name__)
    def test_merge_retains_actual_probe_time(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        register_machine(db_conn)
        aid = _make_agent(db_conn, model_catalog=model_catalog, config_authority=config_authority)
        _set_machine_probe(db_conn, MACHINE, online=True, failures=0)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE machine_probe SET last_probe_at=now()-interval '10 minutes' WHERE machine_name=%s RETURNING last_probe_at",
                (MACHINE,),
            )
            row = cur.fetchone()
            assert row is not None
            observed = row[0]
        db_conn.commit()
        _merge_liveness(pool)
        assert _state(db_conn, aid)[1] == observed

    @pytest.mark.usefixtures(pool.__name__)
    def test_probe_timeout_comes_from_settings(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """The pass's probe budget is `settings.gateway.status_probe_timeout_seconds`
        — the SAME setting the roster's probe reads, so the two probes stay
        aligned by construction — not a hardcoded 3s literal (task #1200: a
        3.0s budget flipped a slow-but-healthy WSL runner offline). A probe
        that outgrows the budget must be a failure (counted toward offline),
        never a success."""
        from base.config import settings

        monkeypatch.setattr(settings.gateway, "status_probe_timeout_seconds", 12.0)
        register_machine(db_conn)
        aid = _make_agent(
            db_conn,
            status="idling",
            lease_s_ahead=600,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        seen: dict[str, object] = {}

        class RecordingProbe(FakeProbe):
            async def __call__(self, target_machine: str, **kwargs: object) -> dict[str, object]:
                seen["timeout_s"] = kwargs.get("timeout_s")
                return await super().__call__(target_machine, **kwargs)

        import asyncio

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=RecordingProbe({MACHINE: True}),
            )
        )
        assert seen["timeout_s"] == 12.0
        assert _state(db_conn, aid)[0] == "online"

    @pytest.mark.usefixtures(pool.__name__)
    def test_lease_expired_idling_goes_offline(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """R1 lease is the process-liveness authority: an idling row whose
        lease expired with the machine up is a dead process -> offline."""
        register_machine(db_conn)
        aid = _make_agent(
            db_conn,
            status="idling",
            lease_s_ahead=-10,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        import asyncio

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({MACHINE: True}),
            )
        )
        state, probed_at = _state(db_conn, aid)
        assert state == "offline"
        assert probed_at is not None

    @pytest.mark.usefixtures(pool.__name__)
    def test_live_idling_stays_online(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        register_machine(db_conn)
        aid = _make_agent(
            db_conn,
            status="idling",
            lease_s_ahead=600,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        import asyncio

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({MACHINE: True}),
            )
        )
        assert _state(db_conn, aid)[0] == "online"

    @pytest.mark.usefixtures(pool.__name__)
    def test_machine_offline_marks_every_agent_offline(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """A host judged offline (>= 2 consecutive failed probes) takes every
        non-terminated row on it offline, lease notwithstanding."""
        register_machine(db_conn)
        _set_machine_probe(db_conn, MACHINE, online=False, failures=_OFFLINE_AFTER_FAILURES)
        aid = _make_agent(
            db_conn,
            status="idling",
            lease_s_ahead=600,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        aid2 = _make_agent(
            db_conn,
            status="running",
            lease_s_ahead=None,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        # Probe success would reset the failure count — this test exercises
        # the merge judgement directly on a pre-set probe state.
        _merge_liveness(pool)
        assert _state(db_conn, aid)[0] == "offline"
        assert _state(db_conn, aid2)[0] == "offline"

    @pytest.mark.usefixtures(pool.__name__)
    def test_single_probe_failure_is_not_offline(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
    ) -> None:
        """One failed probe is a blip: consecutive_failures must reach
        _OFFLINE_AFTER_FAILURES before the machine reads offline."""
        register_machine(db_conn)
        _set_machine_probe(db_conn, MACHINE, online=False, failures=1)
        aid = _make_agent(
            db_conn,
            status="idling",
            lease_s_ahead=600,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        _merge_liveness(pool)
        assert _state(db_conn, aid)[0] == "online"

    @pytest.mark.usefixtures(pool.__name__)
    def test_probe_failures_accumulate_and_reset(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """Consecutive failures accumulate across passes; one success resets."""
        register_machine(db_conn)
        aid = _make_agent(
            db_conn,
            status="idling",
            lease_s_ahead=600,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        import asyncio

        fail = FakeProbe({MACHINE: False})
        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=fail,
            )
        )
        assert _state(db_conn, aid)[0] == "online"  # 1 failure: blip
        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=fail,
            )
        )
        assert _state(db_conn, aid)[0] == "offline"  # 2 failures: offline
        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({MACHINE: True}),
            )
        )
        assert _state(db_conn, aid)[0] == "online"  # success resets

    @pytest.mark.usefixtures(pool.__name__)
    def test_probe_failures_count_up_and_success_resets(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, *, database_gate: ProcessDbGate
    ) -> None:
        register_machine(db_conn)
        import asyncio

        fail = FakeProbe({MACHINE: False})
        for expected in (1, 2, 3):
            asyncio.run(
                run_liveness_pass(
                    Database.from_settings(gate=database_gate),
                    pool,
                    EventBus.from_settings(),
                    probe=fail,
                )
            )
            assert _consecutive_failures(db_conn, MACHINE) == expected

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({MACHINE: True}),
            )
        )
        assert _consecutive_failures(db_conn, MACHINE) == 0

    @pytest.mark.usefixtures(pool.__name__)
    def test_pass_announces_only_liveness_edges(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """A mounted frontend receives online/offline truth without a poll storm."""
        register_machine(db_conn)
        aid = _make_agent(
            db_conn, status="idling", model_catalog=model_catalog, config_authority=config_authority
        )
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
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({MACHINE: True}),
            )
        )
        assert announced == []  # unknown already renders as online

        announced.clear()
        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({MACHINE: True}),
            )
        )
        assert announced == []  # last_probe_at alone never broadcasts the fleet

        fail = FakeProbe({MACHINE: False})
        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=fail,
            )
        )
        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=fail,
            )
        )
        assert announced == [aid]  # online -> offline

        announced.clear()
        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=fail,
            )
        )
        assert announced == []  # offline -> offline is steady state

        announced.clear()
        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({MACHINE: True}),
            )
        )
        assert announced == [aid]  # offline -> online

    @pytest.mark.usefixtures(pool.__name__)
    def test_preclaim_idling_stays_unknown(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """A freshly born idling row has no process claim, so it must not flash
        offline while the launcher is still waiting for the child to claim it."""
        register_machine(db_conn)
        aid = _make_agent(
            db_conn,
            status="idling",
            lease_s_ahead=None,
            claimed=False,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        import asyncio

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({MACHINE: True}),
            )
        )
        assert _state(db_conn, aid)[0] == "unknown"

    @pytest.mark.usefixtures(pool.__name__)
    def test_terminated_rows_are_never_judged(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """A terminated row already renders dead; the pass must not touch it
        (its status is terminal by intent)."""
        register_machine(db_conn)
        aid = _make_agent(
            db_conn,
            status="terminated",
            lease_s_ahead=None,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET liveness_state = 'unknown' WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        import asyncio

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({MACHINE: True}),
            )
        )
        assert _state(db_conn, aid)[0] == "unknown"

    @pytest.mark.usefixtures(pool.__name__)
    def test_unregistered_machine_stays_unknown(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """A row whose machine is not in the machines table is not judged —
        stays 'unknown' (rendered conservatively as online)."""
        aid = _make_agent(
            db_conn,
            status="idling",
            lease_s_ahead=-10,
            machine="ghost-host",
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        import asyncio

        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({}),
            )
        )
        assert _state(db_conn, aid)[0] == "unknown"

    @pytest.mark.usefixtures(pool.__name__)
    def test_merge_is_offline_recovery_ready(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
    ) -> None:
        """A machine that comes back (probe success resets failures) flips its
        agents back online on the next pass — the G5 self-heal path, no
        manual status surgery."""
        register_machine(db_conn)
        aid = _make_agent(
            db_conn,
            status="idling",
            lease_s_ahead=600,
            model_catalog=model_catalog,
            config_authority=config_authority,
        )
        import asyncio

        fail = FakeProbe({MACHINE: False})
        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=fail,
            )
        )
        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=fail,
            )
        )
        assert _state(db_conn, aid)[0] == "offline"
        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({MACHINE: True}),
            )
        )
        assert _state(db_conn, aid)[0] == "online"


@pytest.mark.usefixtures(pool.__name__)
async def test_failed_checkin_is_retried_after_backoff_across_ticks(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A failed check-in stays skipped, then becomes a probe when its window ends."""
    idle_threshold = settings.daemon.heartbeat_idle_threshold_seconds
    aid = spawn_agent(spawner="user", catalog=model_catalog, authority=config_authority)
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
