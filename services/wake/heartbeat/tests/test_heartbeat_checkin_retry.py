import asyncio
import time
from types import SimpleNamespace

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.wake.heartbeat import JITTER_SPAN_S
from services.wake.heartbeat import daemon as heartbeat_daemon
from services.wake.heartbeat.tests.machine_probe_setup import pool as pool


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
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    idle_threshold = settings.daemon.heartbeat_idle_threshold_seconds
    aid = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
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
    monkeypatch.setattr(heartbeat_daemon, "sleep_with_liveness", next_tick)
    monkeypatch.setattr(heartbeat_daemon, "send_heartbeat_checkin", record_checkin)
    monkeypatch.setattr(heartbeat_daemon, "_sweep_backoff_resets", skip_sweep)

    with pytest.raises(asyncio.CancelledError):
        await heartbeat_daemon.dispatch_loop(
            pool, database, event_bus, LoopProgress("dispatch", 60.0)
        )

    assert sent_on_ticks == [0, 3]
