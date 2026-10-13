# pyright: reportUnknownArgumentType=warning
"""No failure receipt gates the hold: `ava start` is the exit, with nothing to settle.

A continuation that failed during the drain is only logged and alerted by the
agent host (`agent_continuation_lost`). The journal carries no receipt, so no gate
refuses on one: the drain keeps waiting for a real continuation (and times out
with the stall report), and `ava start` releases the hold and wakes every
restart pointer without inspecting any agent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest

from base.agents.context.clients import DatabaseFactory
from base.agents.inbox.insert import insert_inbound_message
from base.cluster.machine import MachineIdentity
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.deploy.maintenance import admission, pause_owner
from base.deploy.maintenance.state import MaintenanceHold, MaintenancePhase
from base.deploy.state import host_deploy_state
from base.events.live.bus import EventBus
from cli.database.tests.command_inputs import command_runtime as command_runtime
from ops import agent_pause
from tests.fixtures.unit.identity import machine_identity as machine_identity
from tests.path_scoped.cli_tests import operator_database as operator_database

WHEN = datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
HOLDER = "ops:test:4150"

_MAINTENANCE_PAYLOAD: dict[str, object] = {
    "maintenance": {"holder": HOLDER, "acquired_at": WHEN.isoformat()}
}


@pytest.fixture(autouse=True)
def private_journal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")


def _publish(hold: MaintenanceHold) -> None:
    before = pause_owner.begin_maintenance(HOLDER, WHEN).snapshot
    assert before.maintenance is not None
    pause_owner.change_maintenance(HOLDER, WHEN, before.maintenance, hold)


def test_drain_waits_for_a_real_continuation_instead_of_aborting_early(
    database: Database, machine_identity: MachineIdentity
) -> None:
    """With no latched failure the drain has no fast abort: an undrained member
    ends it at the deadline, with the stall report, and the hold stays."""
    _publish(MaintenanceHold(MaintenancePhase.DRAINING, {1: 11, 2: 22}, drained=(2,)))

    with pytest.raises(TimeoutError, match="drain timed out without force"):
        agent_pause.drain(database, HOLDER, WHEN, 0.3, local_machine=machine_identity)
    assert pause_owner.read().status == "paused"


def _pending_restart(
    db_conn: psycopg.Connection,
    status: str = "idling",
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> tuple[int, int]:
    """A cohort member's durable pointer: an agent row and its pending maintenance restart."""
    agent = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,%s,%s)",
        (agent, status, machine_identity.name()),
    )
    command = insert_inbound_message(
        db_conn,
        agent,
        "",
        "system:maintenance",
        kind="restart",
        payload=_MAINTENANCE_PAYLOAD,
        database=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
    )
    db_conn.commit()
    return agent, command


class _StartHarness:
    """`ava start` over a standing hold, with its release collaborators observable."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        database_factory: DatabaseFactory,
        machine: MachineIdentity,
    ) -> None:
        from base.deploy.lifecycle import start_serving

        self.database_factory = database_factory
        self.machine = machine
        self.woken: list[int] = []
        monkeypatch.setattr(start_serving, "is_serving", lambda: True)
        monkeypatch.setattr(host_deploy_state, "set_posture", MagicMock())
        monkeypatch.setattr(agent_pause, "publish_inbound_wake", self._record_wake)

    def _record_wake(self, _db: object, _bus: object, agent: int, _payload: str) -> bool:
        self.woken.append(agent)
        return True

    def start(self, rc: int = 0) -> int:
        from cli.commands.lifecycle._pause_resume import resume_after_start

        ran: list[bool] = []

        @resume_after_start
        def start(
            operation: pause_owner.PauseOwnerSnapshot | None, database_factory: DatabaseFactory
        ) -> int:
            assert admission.start_authorized(operation)
            ran.append(True)
            return rc

        result = start(None, self.database_factory, self.machine)
        assert ran == [True]
        return result


def test_start_releases_the_hold_and_wakes_every_pending_restart(
    redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection,
    operator_database: DatabaseFactory,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """The restart pointer is intact: the release's resume wakes it."""
    agent, command = _pending_restart(
        db_conn, database_gate=database_gate, machine_identity=machine_identity
    )
    _publish(MaintenanceHold(MaintenancePhase.DRAINING, {agent: command}))
    harness = _StartHarness(
        monkeypatch, database_factory=operator_database, machine=machine_identity
    )

    assert harness.start() == 0

    assert pause_owner.read().status == "resumed"
    assert harness.woken == [agent]


def test_start_releases_even_when_a_restart_pointer_is_gone(
    redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection,
    operator_database: DatabaseFactory,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """A restart pointer that is gone is not settled by `ava start`: the release
    proceeds on the hold alone, and the host's alert was the signal."""
    gone, gone_command = _pending_restart(
        db_conn, database_gate=database_gate, machine_identity=machine_identity
    )
    kept, kept_command = _pending_restart(
        db_conn, database_gate=database_gate, machine_identity=machine_identity
    )
    db_conn.execute("UPDATE inbound_messages SET status='done' WHERE id=%s", (gone_command,))
    db_conn.commit()
    _publish(MaintenanceHold(MaintenancePhase.DRAINING, {gone: gone_command, kept: kept_command}))
    harness = _StartHarness(
        monkeypatch, database_factory=operator_database, machine=machine_identity
    )

    assert harness.start() == 0

    assert pause_owner.read().status == "resumed"
    assert sorted(harness.woken) == sorted([gone, kept])


def test_a_journal_that_still_carries_old_receipts_is_released_by_start(
    redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection,
    operator_database: DatabaseFactory,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """Receipts an earlier release latched are ignored: no gate refuses on them."""
    agent, command = _pending_restart(
        db_conn, database_gate=database_gate, machine_identity=machine_identity
    )
    _publish(MaintenanceHold(MaintenancePhase.DRAINING, {agent: command}))
    path = pause_owner.state_path()
    record = json.loads(path.read_text())
    record["maintenance"]["failures"] = {str(agent): "RuntimeError"}
    record["maintenance"]["undelivered"] = {str(agent): "PoolTimeout"}
    path.write_text(json.dumps(record))
    harness = _StartHarness(
        monkeypatch, database_factory=operator_database, machine=machine_identity
    )

    assert harness.start() == 0

    assert pause_owner.read().status == "resumed"
    assert harness.woken == [agent]


def test_failed_start_keeps_the_hold(
    redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection,
    operator_database: DatabaseFactory,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """A start that failed before the unit serves releases nothing and wakes nothing."""
    agent, command = _pending_restart(
        db_conn, database_gate=database_gate, machine_identity=machine_identity
    )
    _publish(MaintenanceHold(MaintenancePhase.DRAINING, {agent: command}))
    harness = _StartHarness(
        monkeypatch, database_factory=operator_database, machine=machine_identity
    )

    assert harness.start(rc=1) == 1

    current = pause_owner.read()
    assert current.status == "paused"
    assert current.maintenance is not None and current.maintenance.commands == {agent: command}
    assert harness.woken == []
