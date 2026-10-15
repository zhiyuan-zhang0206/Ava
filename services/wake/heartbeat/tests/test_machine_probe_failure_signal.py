from __future__ import annotations

import asyncio

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.contract import payload_keys
from base.events.live.bus import EventBus
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from services.wake.heartbeat.liveness import (
    run_liveness_pass,
)
from services.wake.heartbeat.tests.machine_probe_setup import (
    MACHINE,
    FakeProbe,
    pool,
    register_machine,
)


class TestMachineProbeFailedSignal:
    async def _run(
        self, pool: ConnectionPool, probe: FakeProbe, *, database_gate: ProcessDbGate
    ) -> None:
        await run_liveness_pass(
            Database.from_settings(gate=database_gate), pool, EventBus.from_settings(), probe=probe
        )

    @pytest.mark.usefixtures(pool.__name__)
    def test_each_failed_pass_emits_with_the_running_failure_count(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        emitted_events: EmittedEvents,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        register_machine(db_conn)

        for _ in range(3):
            asyncio.run(self._run(pool, FakeProbe({MACHINE: False}), database_gate=database_gate))
        assert [
            (event.category, event.event_name, event.level, event.attributes)
            for event in emitted_events
        ] == [
            ("telemetry", "machine_probe_failed", "warning", attrs)
            for attrs in (
                {"machine": MACHINE, "consecutive_failures": 1},
                {"machine": MACHINE, "consecutive_failures": 2},
                {"machine": MACHINE, "consecutive_failures": 3},
            )
        ]
        assert set(next(iter(emitted_events)).attributes) == set(
            payload_keys("machine_probe_failed")
        )

    @pytest.mark.usefixtures(pool.__name__)
    def test_reachable_machine_emits_nothing_and_a_new_run_restarts_at_one(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        emitted_events: EmittedEvents,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        register_machine(db_conn)

        asyncio.run(self._run(pool, FakeProbe({MACHINE: False}), database_gate=database_gate))
        asyncio.run(self._run(pool, FakeProbe({MACHINE: True}), database_gate=database_gate))
        asyncio.run(self._run(pool, FakeProbe({MACHINE: False}), database_gate=database_gate))
        assert [event.attributes["consecutive_failures"] for event in emitted_events] == [1, 1]

    @pytest.mark.usefixtures(pool.__name__)
    def test_paused_machine_is_not_probed_and_emits_nothing(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        emitted_events: EmittedEvents,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        register_machine(db_conn, "away")
        register_machine(db_conn, "still-here")
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE machines SET paused_at = now(), pause_reason = 'travel' WHERE name = 'away'"
            )
        db_conn.commit()
        probe = FakeProbe({"still-here": True})

        asyncio.run(self._run(pool, probe, database_gate=database_gate))
        assert probe.calls == ["still-here"]
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM machine_probe WHERE machine_name = 'away'")
            probe_row = cur.fetchone()
            assert probe_row is not None
            (n_probe_rows,) = probe_row
        assert n_probe_rows == 0
        assert len(emitted_events) == 0
