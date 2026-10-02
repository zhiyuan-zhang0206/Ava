# pyright: reportUnknownArgumentType=warning
"""A recorded failure receipt blocks every maintenance gate.

A member whose continuation failed keeps the hold fail-closed at each exit:
preparation retry, drain certification, the `drained` phase transition, the
drain loop, resume and the start path. Each names the sanctioned exit
(`ava maintenance repair`) or refuses, and none of them releases the hold.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from urllib.error import URLError
from uuid import uuid4

import psycopg
import pytest

from base.cluster.machine import machine_name
from base.db import create_agent, insert_inbound_message
from base.deploy.maintenance import admission, cohort, pause_owner
from base.deploy.maintenance.state import MaintenanceHold
from ops import agent_pause, cluster_pause
from ops.agent_pause.probe import HostIdentity, host_identity_or_none

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


def _landed_member(db_conn: psycopg.Connection) -> tuple[int, int]:
    """A drained receipt's durable shape: idling row, claimed applied command."""
    agent = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)",
        (agent, machine_name()),
    )
    command = insert_inbound_message(
        db_conn, agent, "", "system:maintenance", kind="restart", payload=_MAINTENANCE_PAYLOAD
    )
    db_conn.execute(
        "UPDATE inbound_messages SET status='claimed', claimed_at=clock_timestamp(), "
        "applied_at=clock_timestamp(), target_owner=%s, target_generation=%s WHERE id=%s",
        (uuid4(), uuid4(), command),
    )
    db_conn.execute("UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s", (command, agent))
    db_conn.commit()
    return agent, command


def test_prepare_retry_refuses_a_recorded_failure() -> None:
    _publish(
        MaintenanceHold("draining", {1: 11, 2: 22}, drained=(2,), failures={1: "RuntimeError"})
    )
    before = pause_owner.read()
    conn = MagicMock()

    with pytest.raises(RuntimeError, match="maintenance has failed continuations"):
        cohort.prepare(conn, machine="test", host_owner=None, holder=HOLDER, acquired_at=WHEN)

    assert pause_owner.read() == before
    assert conn.mock_calls == []


def test_verify_drained_refuses_a_recorded_failure(db_conn: psycopg.Connection) -> None:
    first = _landed_member(db_conn)
    second = _landed_member(db_conn)
    hold = MaintenanceHold(
        "drained",
        {first[0]: first[1], second[0]: second[1]},
        drained=(first[0], second[0]),
        failures={second[0]: "RuntimeError"},
    )

    with pytest.raises(RuntimeError, match="unfinished or failed"):
        cohort.verify_drained(db_conn, hold)


def test_set_phase_refuses_a_recorded_failure() -> None:
    _publish(
        MaintenanceHold("draining", {1: 11, 2: 22}, drained=(2,), failures={1: "RuntimeError"})
    )

    with pytest.raises(RuntimeError, match="has not fully drained"):
        admission.set_phase(HOLDER, WHEN, "drained")


def test_unpause_names_repair_for_a_recorded_failure() -> None:
    _publish(MaintenanceHold("draining", {1: 11}, failures={1: "RuntimeError"}))

    with pytest.raises(RuntimeError, match="maintenance repair"):
        cluster_pause.unpause_local_cluster()
    assert pause_owner.read().status == "paused"


def test_drain_aborts_on_a_recorded_failure() -> None:
    _publish(
        MaintenanceHold("draining", {1: 11, 2: 22}, drained=(2,), failures={1: "RuntimeError"})
    )

    with pytest.raises(RuntimeError, match="continuations failed; hold retained"):
        agent_pause.drain(HOLDER, WHEN, 1.0)


def test_resume_agents_refuses_a_recorded_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _publish(MaintenanceHold("draining", {1: 11}, failures={1: "RuntimeError"}))
    monkeypatch.setattr(agent_pause, "publish_inbound_wake", MagicMock())

    with pytest.raises(RuntimeError, match="cannot resume failed"):
        agent_pause.resume_agents()


def test_start_path_refuses_a_recorded_failure() -> None:
    from cli.commands.lifecycle._pause_resume import resume_after_start

    _publish(MaintenanceHold("draining", {1: 11}, failures={1: "RuntimeError"}))

    @resume_after_start
    def start() -> int:
        raise AssertionError("start must not run")

    with pytest.raises(RuntimeError, match="start cannot release failed"):
        start()


def test_host_identity_or_none_requires_independent_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = MagicMock(side_effect=URLError(ConnectionRefusedError(111, "Connection refused")))
    monkeypatch.setattr("ops.agent_pause.probe.host_identity", identity)
    monkeypatch.setattr("ops.agent_pause.probe.host_running", lambda: False)
    assert host_identity_or_none() is None
    identity.assert_not_called()

    monkeypatch.setattr("ops.agent_pause.probe.host_running", lambda: True)
    with pytest.raises(URLError):
        host_identity_or_none()

    identity.side_effect = ConnectionRefusedError(111, "Connection refused")
    with pytest.raises(ConnectionRefusedError):
        host_identity_or_none()

    identity.side_effect = None
    identity.return_value = HostIdentity(uuid4(), frozenset({7}))
    assert host_identity_or_none() == identity.return_value


def test_unknown_host_process_evidence_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    def unknown() -> bool:
        raise RuntimeError("cannot identify an unrecorded agent-host home")

    identity = MagicMock()
    monkeypatch.setattr("ops.agent_pause.probe.host_running", unknown)
    monkeypatch.setattr("ops.agent_pause.probe.host_identity", identity)
    with pytest.raises(RuntimeError, match="cannot identify"):
        host_identity_or_none()
    identity.assert_not_called()
