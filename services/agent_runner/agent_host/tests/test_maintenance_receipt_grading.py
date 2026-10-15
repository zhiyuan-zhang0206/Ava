import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from base.agents.inbox.insert import insert_inbound_message
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.deploy.maintenance import admission, pause_owner
from base.deploy.maintenance.state import MaintenanceHold, MaintenancePhase
from base.deploy.state import host_deploy_state
from base.deploy.tests.maintenance_factory import WHEN, maintenance_agent
from base.deploy.tests.maintenance_factory import isolate as isolate
from base.events.live.bus import EventBus
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from ops import agent_pause
from services.agent_runner.agent_host.lifecycle import maintenance as receipts
from services.agent_runner.agent_host.scheduling.pending_wakes import held_maintenance_wakes


class Alerts:
    def __init__(self, records: LogRecords) -> None:
        self._records = records

    @property
    def rows(self) -> list[dict[str, Any]]:
        return [
            {"message": r["message"], **r["extra"]}
            for r in self._records
            if r["level"].name == "ERROR" and r["name"] == receipts.__name__
        ]


@pytest.fixture
def alerts(loguru_records: LogRecords) -> Alerts:
    return Alerts(loguru_records)


def _held() -> MaintenanceHold:
    before = pause_owner.begin_maintenance("grade", WHEN).snapshot
    assert before.maintenance is not None
    hold = MaintenanceHold(MaintenancePhase.DRAINING, {7: 11, 8: 12})
    pause_owner.change_maintenance("grade", WHEN, before.maintenance, hold)
    return hold


def _no_pool() -> AsyncConnectionPool[Any]:
    return cast("AsyncConnectionPool[Any]", MagicMock(side_effect=AssertionError("no db")))


def _owed(
    db_conn: psycopg.Connection[Any],
    machine: MachineIdentity,
    database: Database,
    *,
    applied: bool = False,
) -> tuple[int, int, MaintenanceHold]:
    agent = maintenance_agent(db_conn, machine_name=machine.name())
    command = insert_inbound_message(
        db_conn,
        agent,
        "",
        "system:maintenance",
        kind="restart",
        payload={"maintenance": {"holder": "grade", "acquired_at": WHEN.isoformat()}},
        database=database,
        bus=EventBus.from_settings(),
    )
    if applied:
        db_conn.execute(
            "UPDATE inbound_messages SET status='claimed', claimed_at=clock_timestamp(), "
            "applied_at=clock_timestamp(), target_owner=%s, target_generation=%s WHERE id=%s",
            (uuid4(), uuid4(), command),
        )
    db_conn.execute("UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s", (command, agent))
    db_conn.commit()
    before = pause_owner.begin_maintenance("grade", WHEN).snapshot
    assert before.maintenance is not None
    hold = MaintenanceHold(MaintenancePhase.DRAINING, {agent: command})
    pause_owner.change_maintenance("grade", WHEN, before.maintenance, hold)
    return agent, command, hold


def _command(db_conn: psycopg.Connection[Any], command: int) -> tuple[Any, ...]:
    row = db_conn.execute(
        "SELECT i.status, i.payload->'lifecycle_result', m.lifecycle_command_id "
        "FROM inbound_messages i JOIN agents_meta m ON m.id=i.agent_id WHERE i.id=%s",
        (command,),
    ).fetchone()
    assert row is not None
    return row


@pytest.mark.parametrize(
    "exc",
    [
        PoolTimeout("pool exhausted"),
        psycopg.OperationalError("connection refused"),
        TimeoutError("llm ttft bound"),
        RuntimeError("node failed"),
    ],
    ids=lambda exc: type(exc).__name__,
)
async def test_a_failed_continuation_ends_its_restart_and_alerts_exactly_once(
    redis_url: str,
    exc: BaseException,
    alerts: Alerts,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    machine_identity: MachineIdentity,
    database: Database,
) -> None:
    agent, command, hold = _owed(db_conn, machine_identity, database)

    await receipts.record_failure(aops_pool, agent, exc)

    (row,) = alerts.rows
    assert row["event"] == "agent_continuation_lost"
    assert row["agent_id"] == agent and row["failure_category"] == type(exc).__name__
    assert row["holder"] == "grade" and row["outcome"] == "ended"
    assert _command(db_conn, command) == (
        "done",
        {"outcome": "failed", "reason": "continuation_failed"},
        None,
    )
    assert admission.require_operation("grade", WHEN).maintenance == hold
    for _scan in range(3):
        assert await held_maintenance_wakes(aops_pool) == []
        await receipts.record_failure(aops_pool, agent, exc)
    assert len(alerts.rows) == 1


async def test_other_members_with_an_open_restart_are_still_woken(
    redis_url: str,
    alerts: Alerts,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    machine_identity: MachineIdentity,
    database: Database,
) -> None:
    failed = maintenance_agent(db_conn, machine_name=machine_identity.name())
    healthy = maintenance_agent(db_conn, machine_name=machine_identity.name())
    commands: dict[int, int] = {}
    for agent in (failed, healthy):
        commands[agent] = insert_inbound_message(
            db_conn,
            agent,
            "",
            "system:maintenance",
            kind="restart",
            database=database,
            bus=EventBus.from_settings(),
        )
        db_conn.execute(
            "UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s", (commands[agent], agent)
        )
    db_conn.commit()
    before = pause_owner.begin_maintenance("grade", WHEN).snapshot
    assert before.maintenance is not None
    pause_owner.change_maintenance(
        "grade", WHEN, before.maintenance, MaintenanceHold(MaintenancePhase.DRAINING, commands)
    )

    await receipts.record_failure(aops_pool, failed, RuntimeError("node failed"))

    wakes = await held_maintenance_wakes(aops_pool)
    assert wakes == [healthy]


async def test_an_applied_restart_is_not_rewritten(
    redis_url: str,
    alerts: Alerts,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    machine_identity: MachineIdentity,
    database: Database,
) -> None:
    agent, command, _hold = _owed(db_conn, machine_identity, database, applied=True)

    await receipts.record_failure(aops_pool, agent, RuntimeError("node failed"))

    assert alerts.rows[0]["outcome"] == "applied"
    status, result, pointer = _command(db_conn, command)
    assert (status, result, pointer) == ("claimed", None, command)


async def test_an_unreachable_database_still_alerts_and_keeps_the_restart_open(
    redis_url: str,
    alerts: Alerts,
    db_conn: psycopg.Connection[Any],
    machine_identity: MachineIdentity,
    database: Database,
) -> None:
    agent, command, _hold = _owed(db_conn, machine_identity, database)
    broken = MagicMock()
    broken.connection.side_effect = psycopg.OperationalError("connection refused")

    await receipts.record_failure(cast("Any", broken), agent, RuntimeError("node failed"))

    (row,) = alerts.rows
    assert row["outcome"].startswith("not-ended")
    assert _command(db_conn, command)[0] == "pending"


async def test_a_member_with_an_open_restart_is_run_held(
    redis_url: str,
    alerts: Alerts,
    db_conn: psycopg.Connection[Any],
    machine_identity: MachineIdentity,
    database: Database,
) -> None:
    agent, _command_id, _hold = _owed(db_conn, machine_identity, database)
    controls: list[tuple[int, str]] = []

    async def control(agent_id: int, status: str) -> None:
        controls.append((agent_id, status))

    assert await receipts.run_held(agent, "idling", control) is True
    assert controls == [(agent, "idling")]


async def test_failure_outside_a_hold_is_not_reported(alerts: Alerts) -> None:
    await receipts.record_failure(_no_pool(), 7, RuntimeError("isolated"))
    assert alerts.rows == []
    assert admission.snapshot() is None


def test_a_journal_written_by_an_older_release_still_decodes_without_its_receipts() -> None:
    decoded = MaintenanceHold.decode(
        {
            "phase": "draining",
            "commands": {"7": 11},
            "drained": [],
            "failures": {"7": "RuntimeError"},
            "undelivered": {"7": "PoolTimeout"},
            "parked": [],
        }
    )
    assert decoded == MaintenanceHold(MaintenancePhase.DRAINING, {7: 11})
    assert set(decoded.encode()) == {"phase", "commands", "drained", "parked"}


def test_resume_agents_releases_the_hold(
    monkeypatch: pytest.MonkeyPatch, database: Database, event_bus: EventBus
) -> None:
    from unittest.mock import MagicMock

    from ops.agent_pause import resume_agents

    monkeypatch.setattr(agent_pause, "publish_inbound_wake", MagicMock())
    _held()
    resume_agents(database, event_bus)
    assert pause_owner.read().status == "resumed"


def test_unpause_releases_the_hold(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from unittest.mock import MagicMock

    from ops.cluster_status.pause import unpause_local_cluster

    monkeypatch.setattr(host_deploy_state, "set_posture", MagicMock())
    monkeypatch.setattr(agent_pause, "publish_inbound_wake", MagicMock())
    _held()
    unpause_local_cluster(database, event_bus, local_machine=machine_identity)
    assert pause_owner.read().status == "resumed"


FC10_HOLDER = "fc10-gw:pid17913"
FC10_AT = datetime(2026, 9, 29, 5, 35, 53, 41351, tzinfo=UTC)
FC10_FOREIGN = (3, 8, 9)


def fc10_hold(
    phase: str = "stopping", drained: tuple[int, ...] = (2, 6, 7), parked: tuple[int, ...] = ()
) -> None:
    before = pause_owner.begin_maintenance(FC10_HOLDER, FC10_AT).snapshot
    assert before.maintenance is not None
    hold = MaintenanceHold.decode(
        {
            "phase": phase,
            "commands": {"2": 34, "6": 35, "7": 36},
            "drained": list(drained),
            "parked": list(parked),
        }
    )
    pause_owner.change_maintenance(FC10_HOLDER, FC10_AT, before.maintenance, hold)


_FAILURES = pytest.mark.parametrize(
    "exc",
    [asyncio.CancelledError(), RuntimeError("x"), PoolTimeout("x")],
    ids=lambda exc: type(exc).__name__,
)


@_FAILURES
def test_a_failure_outside_the_captured_cohort_is_not_reported(
    exc: BaseException, alerts: Alerts
) -> None:
    fc10_hold()
    for agent in FC10_FOREIGN:
        asyncio.run(receipts.record_failure(_no_pool(), agent, exc))
    assert alerts.rows == []


@_FAILURES
@pytest.mark.parametrize("phase", ["drained", "stopping", "stopped"])
def test_a_drained_members_failure_after_the_certified_drain_is_not_reported(
    phase: str, exc: BaseException, alerts: Alerts
) -> None:
    fc10_hold(phase)
    asyncio.run(receipts.record_failure(_no_pool(), 2, exc))
    assert alerts.rows == []


def test_before_the_drain_is_certified_only_undrained_members_are_owed(alerts: Alerts) -> None:
    fc10_hold("draining", drained=(6, 7))
    for agent in (2, 6):
        asyncio.run(receipts.record_failure(_no_pool(), agent, asyncio.CancelledError()))
    assert [row["agent_id"] for row in alerts.rows] == [2]


def test_before_the_cohort_is_captured_nothing_is_owed(alerts: Alerts) -> None:
    pause_owner.begin_maintenance(FC10_HOLDER, FC10_AT)
    asyncio.run(receipts.record_failure(_no_pool(), 3, asyncio.CancelledError()))
    assert alerts.rows == []


@pytest.mark.parametrize("phase", ["draining", "stopped"])
def test_a_parked_member_owes_no_continuation(phase: str, alerts: Alerts) -> None:
    before = pause_owner.begin_maintenance(FC10_HOLDER, FC10_AT).snapshot
    assert before.maintenance is not None
    hold = MaintenanceHold.decode(
        {"phase": phase, "commands": {"2": 34}, "drained": [2], "parked": [5]}
    )
    pause_owner.change_maintenance(FC10_HOLDER, FC10_AT, before.maintenance, hold)
    asyncio.run(receipts.record_failure(_no_pool(), 5, asyncio.CancelledError()))
    asyncio.run(receipts.record_failure(_no_pool(), 3, asyncio.CancelledError()))
    assert alerts.rows == []
