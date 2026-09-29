# pyright: reportUnknownArgumentType=warning
"""The reap/failures deadlock unwind (task #4150).

The update straggler reap (task #4016) releases an un-landed cohort member by
CAS-marking its row 'restarting'; the member's in-flight impersonation reads
then lose row ownership and can record a failure *after* the reap already
released it (the 2026-09-20 macmini incidents: failures a subset of reaped,
phase 'drained' and 'draining'). Such a failure has nothing left to repair --
the reap is the member's honest terminal outcome -- so every gate must read
failures through ``MaintenanceHold.unsettled_failures()``. These tests build
both incident shapes and pin the official exits, plus the fail-closed
regressions: a genuinely failed (unreaped) member still blocks everywhere.
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
from base.deploy.maintenance.state import MaintenanceHold, MaintenancePhase
from ops import agent_pause, cluster_pause
from ops.agent_pause.probe import HostIdentity, host_identity_or_none

WHEN = datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
HOLDER = "ops:test:4150"
REAP = "update_straggler_reap"

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


def _current() -> MaintenanceHold:
    current = admission.snapshot()
    assert current is not None and current.maintenance is not None
    return current.maintenance


def _reaped_member(db_conn: psycopg.Connection) -> tuple[int, int]:
    """A reap mark: the row 'restarting' with its maintenance restart un-applied."""
    agent = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_kind,runtime_owner,"
        "runtime_generation,lease_expires_at) VALUES(%s,'restarting',%s,'hosted',%s,%s,"
        "clock_timestamp()+interval '1 minute')",
        (agent, machine_name(), uuid4(), uuid4()),
    )
    command = insert_inbound_message(
        db_conn, agent, "", "system:maintenance", kind="restart", payload=_MAINTENANCE_PAYLOAD
    )
    db_conn.execute("UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s", (command, agent))
    db_conn.commit()
    return agent, command


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


def _incident_hold(
    reaped: tuple[int, int], landed: tuple[int, int], *, phase: MaintenancePhase
) -> MaintenanceHold:
    """The incident's journal shape: a failure recorded around the reap release."""
    return MaintenanceHold(
        phase,
        {reaped[0]: reaped[1], landed[0]: landed[1]},
        drained=(landed[0],),
        reaped={reaped[0]: REAP},
        failures={reaped[0]: "ImpersonationError"},
    )


@pytest.mark.parametrize("phase", ["draining", "drained"])
@pytest.mark.parametrize("unreaped", [False, True])
def test_prepare_retry_uses_only_unsettled_failures(
    phase: MaintenancePhase, unreaped: bool
) -> None:
    failures = {1: "ImpersonationError"}
    if unreaped:
        failures[2] = "RuntimeError"
    hold = MaintenanceHold(phase, {1: 11, 2: 22}, drained=(2,), reaped={1: REAP}, failures=failures)
    _publish(hold)
    before = pause_owner.read()
    conn = MagicMock()

    def retry() -> MaintenanceHold:
        return cohort.prepare(
            conn, machine="test", host_owner=None, holder=HOLDER, acquired_at=WHEN
        )

    if unreaped:
        with pytest.raises(RuntimeError, match="maintenance has failed continuations"):
            retry()
    else:
        assert retry() == hold
    assert pause_owner.read() == before
    assert conn.mock_calls == []


def test_verify_drained_settles_a_failure_recorded_around_the_reap(
    db_conn: psycopg.Connection,
) -> None:
    """Incident shape 1 (drained): the certifying read no longer sees the reaped failure."""
    hold = _incident_hold(_reaped_member(db_conn), _landed_member(db_conn), phase="drained")

    cohort.verify_drained(db_conn, hold)  # must not raise


def test_verify_drained_still_refuses_an_unreaped_failure(
    db_conn: psycopg.Connection,
) -> None:
    reaped = _reaped_member(db_conn)
    landed = _landed_member(db_conn)
    hold = MaintenanceHold(
        "drained",
        {reaped[0]: reaped[1], landed[0]: landed[1]},
        drained=(landed[0],),
        reaped={reaped[0]: REAP},
        failures={landed[0]: "RuntimeError"},
    )

    with pytest.raises(RuntimeError, match="unfinished or failed"):
        cohort.verify_drained(db_conn, hold)


def test_set_phase_reaches_drained_with_settled_failures() -> None:
    _publish(
        MaintenanceHold(
            "draining",
            {1: 11, 2: 22},
            drained=(2,),
            reaped={1: REAP},
            failures={1: "ImpersonationError"},
        )
    )

    admission.set_phase(HOLDER, WHEN, "drained")

    assert _current().phase == "drained"


def test_set_phase_still_refuses_an_unreaped_failure() -> None:
    _publish(
        MaintenanceHold("draining", {1: 11, 2: 22}, drained=(2,), failures={1: "RuntimeError"})
    )

    with pytest.raises(RuntimeError, match="has not fully drained"):
        admission.set_phase(HOLDER, WHEN, "drained")


def test_unpause_releases_a_hold_whose_failures_were_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish(
        MaintenanceHold("draining", {1: 11}, reaped={1: REAP}, failures={1: "ImpersonationError"})
    )
    monkeypatch.setattr("base.deploy.state.host_deploy_state.set_posture", MagicMock())
    monkeypatch.setattr(agent_pause, "publish_inbound_wake", MagicMock())

    cluster_pause.unpause_local_cluster()

    assert pause_owner.read().status == "resumed"


def test_unpause_still_names_repair_for_unreaped_failures() -> None:
    _publish(MaintenanceHold("draining", {1: 11}, failures={1: "RuntimeError"}))

    with pytest.raises(RuntimeError, match="maintenance repair"):
        cluster_pause.unpause_local_cluster()
    assert pause_owner.read().status == "paused"


def test_drain_completes_on_the_draining_incident_shape(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Incident shape 2 (draining): the drain finishes and the phase advances."""
    hold = _incident_hold(_reaped_member(db_conn), _landed_member(db_conn), phase="draining")
    _publish(hold)
    monkeypatch.setattr(agent_pause, "machine_role", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(agent_pause, "host_running", lambda: False)

    agent_pause.drain(HOLDER, WHEN, 1.0)

    assert _current().phase == "drained"


def test_drain_still_aborts_on_an_unreaped_failure() -> None:
    _publish(
        MaintenanceHold(
            "draining", {1: 11, 2: 22}, drained=(2,), reaped={1: REAP}, failures={2: "RuntimeError"}
        )
    )

    with pytest.raises(RuntimeError, match="continuations failed; hold retained"):
        agent_pause.drain(HOLDER, WHEN, 1.0)


def test_resume_agents_releases_with_settled_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    _publish(
        MaintenanceHold("draining", {1: 11}, reaped={1: REAP}, failures={1: "ImpersonationError"})
    )
    monkeypatch.setattr(agent_pause, "publish_inbound_wake", MagicMock())

    agent_pause.resume_agents()

    assert pause_owner.read().status == "resumed"
    admission.require_released("cluster update")  # the update gate is free again


def test_resume_agents_still_refuses_unreaped_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    _publish(MaintenanceHold("draining", {1: 11}, failures={1: "RuntimeError"}))
    monkeypatch.setattr(agent_pause, "publish_inbound_wake", MagicMock())

    with pytest.raises(RuntimeError, match="cannot resume failed"):
        agent_pause.resume_agents()


def test_start_path_reaches_unpause_with_settled_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ava start` no longer refuses the incident journal."""
    from cli.commands.lifecycle._pause_resume import resume_after_start

    _publish(
        MaintenanceHold("draining", {1: 11}, reaped={1: REAP}, failures={1: "ImpersonationError"})
    )
    steps: list[str] = []
    monkeypatch.setattr("base.deploy.lifecycle.start_serving.is_serving", lambda: True)
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", lambda: steps.append("unpause"))

    @resume_after_start
    def start() -> int:
        steps.append("start")
        return 0

    assert start() == 0
    assert steps == ["start", "unpause"]


def test_start_path_still_refuses_unreaped_failures() -> None:
    from cli.commands.lifecycle._pause_resume import resume_after_start

    _publish(MaintenanceHold("draining", {1: 11}, failures={1: "RuntimeError"}))

    @resume_after_start
    def start() -> int:
        raise AssertionError("start must not run")

    with pytest.raises(RuntimeError, match="start cannot release failed"):
        start()


def test_reap_receipt_supersedes_a_racing_failure() -> None:
    """The same committed reap is accepted in either receipt arrival order."""
    _publish(MaintenanceHold("draining", {1: 11, 2: 22}, reaped={1: REAP}))

    admission.record_failure(1, "ImpersonationError")
    admission.record_failure(2, "ImpersonationError")
    assert _current().unsettled_failures() == {2: "ImpersonationError"}

    admission.record_reaped(1, REAP)
    admission.record_reaped(2, REAP)

    assert _current().reaped == {1: REAP, 2: REAP}
    assert _current().failures == {2: "ImpersonationError"}
    assert _current().unsettled_failures() == {}


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
