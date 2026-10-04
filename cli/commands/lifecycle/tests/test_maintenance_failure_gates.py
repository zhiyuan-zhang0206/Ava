# pyright: reportUnknownArgumentType=warning
"""A recorded failure receipt blocks every maintenance gate except `ava start`.

A member whose continuation failed keeps the hold fail-closed at each gate:
preparation retry, drain certification, the `drained` phase transition, the
drain loop and resume. Each names `ava start` or refuses, and none of them
releases the hold. `ava start` is the one exit: once the unit serves, it
re-delivers each failed continuation, reports and notifies for any it cannot
deliver, and releases the hold.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest

from base.cluster.machine import machine_name
from base.db import Database, create_agent, insert_inbound_message
from base.deploy.maintenance import admission, cohort, pause_owner
from base.deploy.maintenance.state import MaintenanceHold
from base.events.live.bus import EventBus
from ops import agent_pause, cluster_pause

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
        db_conn,
        agent,
        "",
        "system:maintenance",
        kind="restart",
        payload=_MAINTENANCE_PAYLOAD,
        database=Database.from_settings(),
        bus=EventBus.from_settings(),
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


def test_unpause_names_start_for_a_recorded_failure(
    database: Database, event_bus: EventBus
) -> None:
    _publish(MaintenanceHold("draining", {1: 11}, failures={1: "RuntimeError"}))

    with pytest.raises(RuntimeError, match="run `ava start`"):
        cluster_pause.unpause_local_cluster(database, event_bus)
    assert pause_owner.read().status == "paused"


def test_drain_aborts_on_a_recorded_failure(database: Database) -> None:
    _publish(
        MaintenanceHold("draining", {1: 11, 2: 22}, drained=(2,), failures={1: "RuntimeError"})
    )

    with pytest.raises(RuntimeError, match="continuations failed; hold retained"):
        agent_pause.drain(database, HOLDER, WHEN, 1.0)


def test_resume_agents_refuses_a_recorded_failure(
    monkeypatch: pytest.MonkeyPatch, database: Database, event_bus: EventBus
) -> None:
    _publish(MaintenanceHold("draining", {1: 11}, failures={1: "RuntimeError"}))
    monkeypatch.setattr(agent_pause, "publish_inbound_wake", MagicMock())

    with pytest.raises(RuntimeError, match="cannot resume failed"):
        agent_pause.resume_agents(database, event_bus)


def _pending_restart(db_conn: psycopg.Connection, status: str = "idling") -> tuple[int, int]:
    """A cohort member's durable pointer: an agent row and its pending maintenance restart."""
    agent = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,%s,%s)",
        (agent, status, machine_name()),
    )
    command = insert_inbound_message(
        db_conn,
        agent,
        "",
        "system:maintenance",
        kind="restart",
        payload=_MAINTENANCE_PAYLOAD,
        database=Database.from_settings(),
        bus=EventBus.from_settings(),
    )
    db_conn.commit()
    return agent, command


class _StartHarness:
    """`ava start` over a standing hold, with its release collaborators observable."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from base.deploy.lifecycle import start_serving

        self.woken: list[int] = []
        self.alerts: list[dict[str, object]] = []
        self.logger = MagicMock()
        monkeypatch.setattr(start_serving, "is_serving", lambda: True)
        monkeypatch.setattr("base.deploy.state.host_deploy_state.set_posture", MagicMock())
        monkeypatch.setattr("ops.agent_pause.publish_inbound_wake", self._record_wake)
        monkeypatch.setattr("cli.commands.lifecycle._failed_receipts.logger", self.logger)
        monkeypatch.setattr("cli.commands._probe._alert_db_connect", MagicMock())
        monkeypatch.setattr("cli.commands._probe._alert_upsert_and_maybe_im", self._record_alert)

    def _record_wake(self, _db: object, _bus: object, agent: int, _payload: str) -> bool:
        self.woken.append(agent)
        return True

    def _record_alert(self, _conn: object, alert: dict[str, object], *, im_enabled: bool) -> None:
        assert im_enabled
        self.alerts.append(alert)

    def start(self, rc: int = 0) -> int:
        from cli.commands.lifecycle._pause_resume import resume_after_start

        ran: list[bool] = []

        @resume_after_start
        def start() -> int:
            assert admission.start_authorized()
            ran.append(True)
            return rc

        result = start()
        assert ran == [True]
        return result


def test_start_redelivers_a_failed_continuation_and_releases_the_hold(
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    """The failed member's restart pointer is intact: no error, no notice; the release wakes it."""
    agent, command = _pending_restart(db_conn)
    _publish(MaintenanceHold("draining", {agent: command}, failures={agent: "RuntimeError"}))
    harness = _StartHarness(monkeypatch)

    assert harness.start() == 0

    assert pause_owner.read().status == "resumed"
    assert harness.woken == [agent]
    harness.logger.error.assert_not_called()
    assert harness.alerts == []


def test_start_reports_and_notifies_a_continuation_it_cannot_redeliver(
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    """A restart pointer that is gone is logged at ERROR and told to the owner; start still goes."""
    lost, lost_command = _pending_restart(db_conn)
    kept, kept_command = _pending_restart(db_conn)
    db_conn.execute("UPDATE inbound_messages SET status='done' WHERE id=%s", (lost_command,))
    db_conn.commit()
    _publish(
        MaintenanceHold(
            "draining",
            {lost: lost_command, kept: kept_command},
            failures={lost: "RuntimeError", kept: "ValueError"},
        )
    )
    harness = _StartHarness(monkeypatch)

    assert harness.start() == 0

    assert pause_owner.read().status == "resumed"
    assert sorted(harness.woken) == sorted([lost, kept])
    harness.logger.error.assert_called_once()
    assert harness.logger.error.call_args.kwargs["agent_id"] == lost
    assert harness.logger.error.call_args.kwargs["category"] == "RuntimeError"
    assert [alert["labels"]["agent"] for alert in harness.alerts] == [str(lost)]  # type: ignore[index]
    summary = harness.alerts[0]["annotations"]["summary"]  # type: ignore[index]
    assert f"agent {lost}" in summary
    assert f"agent {kept}" not in summary


def test_start_does_not_revive_a_terminated_agent_whose_continuation_failed(
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    agent, command = _pending_restart(db_conn, status="terminated")
    db_conn.execute("UPDATE inbound_messages SET status='done' WHERE id=%s", (command,))
    db_conn.commit()
    _publish(MaintenanceHold("draining", {agent: command}, failures={agent: "RuntimeError"}))
    harness = _StartHarness(monkeypatch)

    assert harness.start() == 0

    assert pause_owner.read().status == "resumed"
    harness.logger.error.assert_not_called()
    assert harness.alerts == []


def test_failed_start_keeps_the_hold_and_its_receipts(
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    """Receipts are settled only once the unit serves: a start that failed leaves them for the retry."""
    agent, command = _pending_restart(db_conn)
    _publish(MaintenanceHold("draining", {agent: command}, failures={agent: "RuntimeError"}))
    harness = _StartHarness(monkeypatch)

    assert harness.start(rc=1) == 1

    current = pause_owner.read()
    assert current.status == "paused"
    assert current.maintenance is not None
    assert current.maintenance.failures == {agent: "RuntimeError"}
    assert harness.woken == []


def test_unreadable_pointer_check_keeps_the_failure_receipts(
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    agent, command = _pending_restart(db_conn)
    _publish(MaintenanceHold("draining", {agent: command}, failures={agent: "RuntimeError"}))
    harness = _StartHarness(monkeypatch)

    def unreachable() -> None:
        raise ConnectionError("database unreachable")

    monkeypatch.setattr(
        "cli.commands.lifecycle._failed_receipts.Database.from_settings", unreachable
    )

    with pytest.raises(ConnectionError):
        harness.start()

    current = pause_owner.read()
    assert current.status == "paused"
    assert current.maintenance is not None
    assert current.maintenance.failures == {agent: "RuntimeError"}
