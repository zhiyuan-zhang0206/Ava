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

from ops import agent_pause, cluster_pause
from ops.agent_pause_probe import HostIdentity, host_identity_or_none
from shared import maintenance, maintenance_cohort, pause_owner
from shared.db import create_agent, insert_inbound_message
from shared.machine import machine_name
from shared.maintenance_state import MaintenanceHold, MaintenancePhase

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
    before = pause_owner.begin_maintenance(HOLDER, WHEN)
    assert before.maintenance is not None
    pause_owner.change_maintenance(HOLDER, WHEN, before.maintenance, hold)


def _current() -> MaintenanceHold:
    current = maintenance.snapshot()
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


def test_verify_drained_settles_a_failure_recorded_around_the_reap(
    db_conn: psycopg.Connection,
) -> None:
    """Incident shape 1 (drained): the certifying read no longer sees the reaped failure."""
    hold = _incident_hold(_reaped_member(db_conn), _landed_member(db_conn), phase="drained")

    maintenance_cohort.verify_drained(db_conn, hold)  # must not raise


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
        maintenance_cohort.verify_drained(db_conn, hold)


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

    maintenance.set_phase(HOLDER, WHEN, "drained")

    assert _current().phase == "drained"


def test_set_phase_still_refuses_an_unreaped_failure() -> None:
    _publish(
        MaintenanceHold("draining", {1: 11, 2: 22}, drained=(2,), failures={1: "RuntimeError"})
    )

    with pytest.raises(RuntimeError, match="has not fully drained"):
        maintenance.set_phase(HOLDER, WHEN, "drained")


def test_resume_refusal_ignores_settled_failures() -> None:
    _publish(
        MaintenanceHold("draining", {1: 11}, reaped={1: REAP}, failures={1: "ImpersonationError"})
    )

    assert cluster_pause.local_resume_refusal() is None


def test_resume_refusal_still_names_repair_for_unreaped_failures() -> None:
    _publish(MaintenanceHold("draining", {1: 11}, failures={1: "RuntimeError"}))

    refusal = cluster_pause.local_resume_refusal()

    assert refusal is not None and "maintenance repair" in refusal


def test_drain_completes_on_the_draining_incident_shape(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Incident shape 2 (draining): the drain finishes and the phase advances."""
    hold = _incident_hold(_reaped_member(db_conn), _landed_member(db_conn), phase="draining")
    _publish(hold)
    monkeypatch.setattr(agent_pause, "machine_role", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(agent_pause, "host_running", lambda: False)

    agent_pause._drain(HOLDER, WHEN, 1.0)

    assert _current().phase == "drained"


def test_drain_still_aborts_on_an_unreaped_failure() -> None:
    _publish(
        MaintenanceHold(
            "draining", {1: 11, 2: 22}, drained=(2,), reaped={1: REAP}, failures={2: "RuntimeError"}
        )
    )

    with pytest.raises(RuntimeError, match="continuations failed; hold retained"):
        agent_pause._drain(HOLDER, WHEN, 1.0)


def test_resume_agents_releases_with_settled_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    _publish(
        MaintenanceHold("draining", {1: 11}, reaped={1: REAP}, failures={1: "ImpersonationError"})
    )
    monkeypatch.setattr(agent_pause, "publish_inbound_wake", MagicMock())

    agent_pause.resume_agents()

    assert pause_owner.read().status == "resumed"
    maintenance.require_released("cluster update")  # the update gate is free again


def test_resume_agents_still_refuses_unreaped_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    _publish(MaintenanceHold("draining", {1: 11}, failures={1: "RuntimeError"}))
    monkeypatch.setattr(agent_pause, "publish_inbound_wake", MagicMock())

    with pytest.raises(RuntimeError, match="cannot resume failed"):
        agent_pause.resume_agents()


def test_start_path_reaches_unpause_with_settled_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ava start` no longer refuses the incident journal."""
    from cli.commands._pause_resume import resume_after_start

    _publish(
        MaintenanceHold("draining", {1: 11}, reaped={1: REAP}, failures={1: "ImpersonationError"})
    )
    steps: list[str] = []
    monkeypatch.setattr("shared.start_serving.is_serving", lambda: True)
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", lambda: steps.append("unpause"))

    @resume_after_start
    def start() -> int:
        steps.append("start")
        return 0

    assert start() == 0
    assert steps == ["start", "unpause"]


def test_start_path_still_refuses_unreaped_failures() -> None:
    from cli.commands._pause_resume import resume_after_start

    _publish(MaintenanceHold("draining", {1: 11}, failures={1: "RuntimeError"}))

    @resume_after_start
    def start() -> int:
        raise AssertionError("start must not run")

    with pytest.raises(RuntimeError, match="start cannot release failed"):
        start()


def test_post_reap_failures_never_latch_and_reap_stays_guarded() -> None:
    """The symmetric writer guard: the reap release wins; the mirror keeps guarding reaps."""
    _publish(MaintenanceHold("draining", {1: 11, 2: 22}, reaped={1: REAP}))

    maintenance.record_failure(1, "ImpersonationError")  # reaped member: dropped
    maintenance.record_failure(2, "RuntimeError")  # live member: latched

    assert _current().failures == {2: "RuntimeError"}
    maintenance.record_reaped(1, REAP)  # legacy both-state re-mark: idempotent, no raise
    with pytest.raises(RuntimeError, match="failed continuation cannot be reaped"):
        maintenance.record_reaped(2, REAP)


def test_host_identity_or_none_degrades_only_on_a_refused_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused() -> HostIdentity:
        raise URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr("ops.agent_pause_probe.host_identity", refused)
    assert host_identity_or_none() is None

    def direct_refusal() -> HostIdentity:
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr("ops.agent_pause_probe.host_identity", direct_refusal)
    assert host_identity_or_none() is None

    def wedged() -> HostIdentity:
        raise URLError(TimeoutError("timed out"))

    monkeypatch.setattr("ops.agent_pause_probe.host_identity", wedged)
    with pytest.raises(URLError):
        host_identity_or_none()
