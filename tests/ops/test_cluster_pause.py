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

from ops import agent_pause, cluster_pause
from ops.cluster_pause import unpause_local_cluster as _real_unpause_local_cluster
from shared import maintenance, pause_owner
from shared.db import create_agent, insert_inbound_message
from shared.host_deploy_state import HostDeployState
from shared.machine import machine_name


@pytest.fixture
def posture(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every `set_posture` call so the pairing is observable without a DB."""
    calls: list[str] = []
    monkeypatch.setattr("shared.host_deploy_state.set_posture", calls.append)
    return calls


@pytest.fixture(autouse=True)
def local_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _StubBackend:
    """Keep each operation's journal private and leave real DB drain intact."""
    backend = _StubBackend()
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")
    monkeypatch.setattr(agent_pause, "host_running", lambda: False)
    # The real unpause is safe here: the backend records every possible spawn.
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", _real_unpause_local_cluster)
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
    now = datetime.now(UTC)
    return HostDeployState(
        machine="win",
        posture=posture,
        updated_at=now,
        updater_lease_expires_at=None,
    )


def test_is_paused_judges_a_pre_read_state_without_another_db_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected_read() -> HostDeployState | None:
        raise AssertionError("is_paused re-read host deploy state")

    monkeypatch.setattr("shared.host_deploy_state.read", _unexpected_read)

    assert cluster_pause.is_paused(_state("paused")) is True
    assert cluster_pause.is_paused(_state("idle")) is False
    assert cluster_pause.is_paused(None) is False


def test_is_paused_without_an_argument_still_reads_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = 0

    def _read() -> HostDeployState:
        nonlocal reads
        reads += 1
        return _state("paused")

    monkeypatch.setattr("shared.host_deploy_state.read", _read)

    assert cluster_pause.is_paused() is True
    assert reads == 1


def test_pause_holds_admission_without_closing_dependencies(
    posture: list[str], local_runtime: _StubBackend
) -> None:
    cluster_pause.pause_local_cluster()
    current = maintenance.snapshot()
    assert current is not None and current.maintenance is not None
    assert current.maintenance.phase == "drained"
    assert posture == [], "in-flight SDK requests still need the gateway"
    assert local_runtime.has_answer and local_runtime.killed == []


def test_unpause_writes_idle_posture(posture: list[str]) -> None:
    cluster_pause.pause_local_cluster()

    cluster_pause.unpause_local_cluster()

    assert posture == ["idle"]
    assert not maintenance.held()


def test_unpause_without_pause_is_a_noop(posture: list[str]) -> None:
    """The compensating resume can arrive at a host that never paused (or already
    recovered); writing idle over an idle row is a no-op, never an error."""
    cluster_pause.unpause_local_cluster()
    assert posture == ["idle"]


def test_pause_twice_then_unpause_once_clears(posture: list[str]) -> None:
    """Idempotent pause: a second pause (e.g. a repeat Phase-A delivery) must not
    leave the host paused after a single unpause."""
    cluster_pause.pause_local_cluster()
    first = pause_owner.read()
    cluster_pause.pause_local_cluster()
    assert pause_owner.read() == first
    cluster_pause.unpause_local_cluster()
    assert posture == ["idle"]
    assert not maintenance.held()


def test_pause_preserves_unclaimed_work_and_terminated_intent(
    db_conn: psycopg.Connection, posture: list[str], local_runtime: _StubBackend
) -> None:
    agent, terminated = create_agent(db_conn), create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s),(%s,'terminated',%s)",
        (agent, machine_name(), terminated, machine_name()),
    )
    db_conn.commit()
    message = insert_inbound_message(db_conn, agent, "queued work", "user")

    cluster_pause.pause_local_cluster()

    current = maintenance.snapshot()
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
) -> None:
    agent = create_agent(db_conn)
    from ops.agent_pause_probe import HostIdentity

    owner, generation = uuid4(), uuid4()
    monkeypatch.setattr(agent_pause, "host_running", lambda: True)
    monkeypatch.setattr(
        agent_pause, "host_identity", lambda: HostIdentity(owner, frozenset({agent}))
    )
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_kind,runtime_owner,"
        "runtime_generation,lease_expires_at) VALUES(%s,'running',%s,'hosted',"
        "%s,%s,clock_timestamp()+interval '1 minute')",
        (agent, machine_name(), owner, generation),
    )
    db_conn.commit()
    monkeypatch.setattr("shared.config.settings.gateway.update_quiesce_timeout_seconds", 0.01)

    with pytest.raises(TimeoutError, match="without force"):
        cluster_pause.pause_local_cluster()

    current = maintenance.snapshot()
    assert current is not None and current.maintenance is not None
    assert current.maintenance.phase == "draining"
    assert current.maintenance.drained == ()
    command = current.maintenance.commands[agent]
    assert db_conn.execute(
        "SELECT kind,status FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone() == ("restart", "pending")
    assert db_conn.execute(
        "SELECT status,runtime_owner,runtime_generation FROM agents_meta WHERE id=%s", (agent,)
    ).fetchone() == ("running", owner, generation)
    assert posture == [] and local_runtime.has_answer and local_runtime.killed == []


def test_unpause_changes_no_service_sessions(
    posture: list[str], local_runtime: _StubBackend
) -> None:
    """Service startup belongs to ava start; unpause only releases native admission."""
    local_runtime.has_answer = False
    cluster_pause.unpause_local_cluster()
    assert posture == ["idle"]
    assert local_runtime.spawned == local_runtime.killed == []
