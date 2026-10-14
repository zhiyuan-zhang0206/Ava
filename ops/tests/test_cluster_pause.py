# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
"""Native pause preserves dependencies and pending work until service shutdown.

Use real PostgreSQL and a private admission journal. The recording backend
records service operations without launching real daemons.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

import base.sessions.backend as sessions_backend
from base.agents.inbox.insert import insert_inbound_message
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database, create_agent
from base.deploy.lifecycle import start_serving
from base.deploy.maintenance import admission, pause_owner
from base.deploy.maintenance.state import MaintenancePhase
from base.deploy.state import host_deploy_state
from base.deploy.state.host_deploy_state import HostDeployState
from base.events.live.bus import EventBus
from ops import agent_pause
from ops.cluster_status import pause as cluster_pause
from ops.cluster_status.pause import unpause_local_cluster as _real_unpause_local_cluster


@pytest.fixture
def posture(monkeypatch: pytest.MonkeyPatch, machine_identity: MachineIdentity) -> list[str]:
    """Record every `set_posture` call so the pairing is observable without a DB."""
    calls: list[str] = []

    def record(_db: Database, value: str, *, machine: str) -> None:
        assert machine == machine_identity.name()
        calls.append(value)

    monkeypatch.setattr(host_deploy_state, "set_posture", record)
    return calls


@pytest.fixture(autouse=True)
def local_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _StubBackend:
    """Keep each operation's journal private and leave real DB drain intact."""
    backend = _StubBackend()
    monkeypatch.setattr(sessions_backend, "get_backend", lambda: backend)
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")
    monkeypatch.setattr(agent_pause, "host_running", lambda: False)
    # The real unpause is safe here: the backend records every possible spawn.
    monkeypatch.setattr(cluster_pause, "unpause_local_cluster", _real_unpause_local_cluster)
    return backend


class _StubBackend:
    def __init__(self) -> None:
        self.has_answer = True
        self.spawned: list[str] = []
        self.killed: list[str] = []

    def has_session(self, _name: str) -> bool:
        return self.has_answer

    def new_session(self, name: str, _cmd: str, _cwd: object, *, env: object, **_: object) -> bool:
        self.spawned.append(name)
        return True

    def kill_session(
        self, name: str, graceful: bool = False, expected: bool = False
    ) -> tuple[bool, str]:
        self.killed.append(name)
        self.has_answer = False
        return True, "stub"


def _state(posture: str) -> HostDeployState:
    return HostDeployState(machine="runner-a", posture=posture, updated_at=datetime.now(UTC))


def test_is_paused_judges_a_pre_read_state_without_another_db_read(
    monkeypatch: pytest.MonkeyPatch, database: Database, *, machine_identity: MachineIdentity
) -> None:
    def _unexpected_read(_db: object) -> HostDeployState | None:
        raise AssertionError("is_paused re-read host deploy state")

    monkeypatch.setattr(host_deploy_state, "read", _unexpected_read)

    def _no_name() -> str:
        raise AssertionError("is_paused resolved the name for a pre-read state")

    assert cluster_pause.is_paused(database, _state("paused"), machine_name=_no_name) is True
    assert cluster_pause.is_paused(database, _state("idle"), machine_name=_no_name) is False
    assert cluster_pause.is_paused(database, None, machine_name=_no_name) is False


def test_is_paused_without_an_argument_still_reads_fresh(
    monkeypatch: pytest.MonkeyPatch, database: Database, *, machine_identity: MachineIdentity
) -> None:
    reads = 0

    def _read(_db: object, machine: str) -> HostDeployState:
        assert machine == machine_identity.name()
        nonlocal reads
        reads += 1
        return _state("paused")

    monkeypatch.setattr(host_deploy_state, "read", _read)

    assert cluster_pause.is_paused(database, machine_name=machine_identity.name) is True
    assert reads == 1


def test_pause_holds_admission_without_closing_dependencies(
    posture: list[str],
    local_runtime: _StubBackend,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    agent_pause.pause_agents(database, event_bus, local_machine=machine_identity)
    current = admission.snapshot()
    assert current is not None and current.maintenance is not None
    assert current.maintenance.phase == MaintenancePhase.DRAINED
    assert posture == [], "in-flight SDK requests still need the gateway"
    assert local_runtime.has_answer and local_runtime.killed == []


def test_unpause_writes_idle_posture(
    posture: list[str],
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    agent_pause.pause_agents(database, event_bus, local_machine=machine_identity)

    cluster_pause.unpause_local_cluster(database, event_bus, local_machine=machine_identity)

    assert posture == ["idle"]
    assert not admission.held()


def test_unpause_without_pause_is_a_noop(
    posture: list[str],
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """The compensating resume can arrive at a host that never paused (or already
    recovered); writing idle over an idle row is a no-op, never an error."""
    cluster_pause.unpause_local_cluster(database, event_bus, local_machine=machine_identity)
    assert posture == ["idle"]


def test_pause_twice_then_unpause_once_clears(
    posture: list[str],
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """Idempotent pause: a second pause (e.g. a repeat Phase-A delivery) must not
    leave the host paused after a single unpause."""
    agent_pause.pause_agents(database, event_bus, local_machine=machine_identity)
    first = pause_owner.read()
    agent_pause.pause_agents(database, event_bus, local_machine=machine_identity)
    assert pause_owner.read() == first
    cluster_pause.unpause_local_cluster(database, event_bus, local_machine=machine_identity)
    assert posture == ["idle"]
    assert not admission.held()


def test_pause_preserves_unclaimed_work_and_terminated_intent(
    db_conn: psycopg.Connection,
    posture: list[str],
    local_runtime: _StubBackend,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    agent, terminated = create_agent(db_conn), create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s),(%s,'terminated',%s)",
        (agent, machine_identity.name(), terminated, machine_identity.name()),
    )
    db_conn.commit()
    message = insert_inbound_message(
        db_conn, agent, "queued work", "user", bus=event_bus, database=database
    )

    agent_pause.pause_agents(database, event_bus, local_machine=machine_identity)

    current = admission.snapshot()
    assert current is not None and current.maintenance is not None
    assert current.maintenance.parked == (agent,)
    assert current.maintenance.commands == {}
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (message,)
    ).fetchone() == ("pending",)
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (terminated,)
    ).fetchone() == ("terminated",)
    assert posture == [] and local_runtime.killed == []


def test_drain_timeout_retains_hold_and_action_dependencies(
    db_conn: psycopg.Connection,
    posture: list[str],
    local_runtime: _StubBackend,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    agent = create_agent(db_conn)
    from ops.agent_pause.probe import HostIdentity

    owner, generation = uuid4(), uuid4()
    monkeypatch.setattr(agent_pause, "host_running", lambda: True)
    monkeypatch.setattr(
        agent_pause, "host_identity", lambda: HostIdentity(owner, frozenset({agent}))
    )
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_kind,runtime_owner,"
        "runtime_generation,lease_expires_at) VALUES(%s,'running',%s,'hosted',"
        "%s,%s,clock_timestamp()+interval '1 minute')",
        (agent, machine_identity.name(), owner, generation),
    )
    db_conn.commit()

    with pytest.raises(TimeoutError, match="without force") as raised:
        agent_pause.pause_agents(database, event_bus, 0.01, local_machine=machine_identity)

    current = admission.snapshot()
    assert current is not None and current.maintenance is not None
    assert current.maintenance.phase == MaintenancePhase.DRAINING
    assert current.maintenance.drained == ()
    command = current.maintenance.commands[agent]
    # Issue #2159: the timeout names per-agent delivery state, row fences and
    # the live host's view instead of the bare agent list the operator had.
    message = str(raised.value)
    assert f"agent {agent}: command {command} pending, applied=no" in message
    assert "lease_fresh=yes" in message and "resources_settled=yes" in message
    assert "host active=yes" in message
    assert db_conn.execute(
        "SELECT kind,status FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone() == ("restart", "pending")
    assert db_conn.execute(
        "SELECT status,runtime_owner,runtime_generation FROM agents_meta WHERE id=%s", (agent,)
    ).fetchone() == ("running", owner, generation)
    assert posture == [] and local_runtime.has_answer and local_runtime.killed == []


def test_stall_report_names_the_predecessor_owner_fence(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """An unowned row must be reported as the predecessor's fence, not guessed at.

    The drain's agent list said nothing about WHY a successor boot (this host)
    was not consuming a row left by its predecessor; the report must name the
    owner mismatch, which is the fence that requires explicit resolution.
    """
    from base.deploy.maintenance.state import MaintenanceHold
    from ops.agent_pause import stall_report
    from ops.agent_pause.probe import HostIdentity

    agent = create_agent(db_conn)
    predecessor, successor, generation = uuid4(), uuid4(), uuid4()
    command = insert_inbound_message(
        db_conn, agent, "", "system:maintenance", kind="restart", bus=event_bus, database=database
    )
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_kind,runtime_owner,"
        "runtime_generation,lease_expires_at) VALUES(%s,'idling',%s,'hosted',"
        "%s,%s,clock_timestamp()+interval '1 minute')",
        (agent, machine_identity.name(), predecessor, generation),
    )
    db_conn.commit()
    monkeypatch.setattr(agent_pause, "host_running", lambda: True)
    monkeypatch.setattr(agent_pause, "host_identity", lambda: HostIdentity(successor, frozenset()))

    report = stall_report(
        database,
        MaintenanceHold(MaintenancePhase.DRAINING, {agent: command}),
        [agent],
        local_machine=machine_identity,
    )

    assert f"owner={predecessor}" in report
    assert f"not the live boot {successor}" in report
    assert "successor host cannot certify its predecessor flushed" in report


def test_unpause_changes_no_service_sessions(
    posture: list[str],
    local_runtime: _StubBackend,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """Service startup belongs to ava start; unpause only releases native admission."""
    local_runtime.has_answer = False
    cluster_pause.unpause_local_cluster(database, event_bus, local_machine=machine_identity)
    assert posture == ["idle"]
    assert local_runtime.spawned == local_runtime.killed == []


def test_unpause_refuses_a_held_unit_whose_services_stopped(
    monkeypatch: pytest.MonkeyPatch,
    posture: list[str],
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """A held unit whose services stopped resumes only after `ava start` passes readiness."""
    from base.deploy.maintenance.state import MaintenanceHold

    when = datetime(2026, 9, 10, tzinfo=UTC)
    pause_owner.begin_maintenance("runner-a:pid1", when)
    pause_owner.change_maintenance(
        "runner-a:pid1", when, MaintenanceHold(), MaintenanceHold(MaintenancePhase.STOPPING)
    )
    monkeypatch.setattr(start_serving, "is_serving", lambda: False)

    with pytest.raises(RuntimeError) as raised:
        cluster_pause.unpause_local_cluster(database, event_bus, local_machine=machine_identity)
    assert str(raised.value) == "services have stopped; ava start must pass readiness before resume"
    assert posture == []
