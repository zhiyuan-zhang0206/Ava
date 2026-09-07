"""Regression coverage for batched reads in the host status snapshot."""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from ops import cluster_status
from ops.rpc_schemas import SessionInfo
from shared.cluster_lock import DeployLease
from shared.host_deploy_state import HostDeployState
from shared.resource_sample import ResourceSample

_RESOURCE = ResourceSample(
    ts=1.0,
    cpu_pct=2.0,
    mem_used_gb=3.0,
    mem_total_gb=4.0,
    mem_pct=5.0,
    disk_used_gb=6.0,
    disk_total_gb=7.0,
    disk_pct=8.0,
)


class _Pool:
    def __init__(self, conn: object, *, error: Exception | None = None) -> None:
        self.conn = conn
        self.error = error
        self.timeouts: list[float] = []

    @contextmanager
    def connection(self, *, timeout: float) -> Generator[object, None, None]:
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        yield self.conn


@pytest.fixture
def snapshot_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[HostDeployState, DeployLease]:
    """Keep the snapshot focused on DB bundling and resource sampling."""
    now = datetime.now(UTC)
    state = HostDeployState(
        machine="win",
        posture="paused",
        updated_at=now,
        updater_lease_expires_at=now + timedelta(minutes=5),
        paused_at=now - timedelta(seconds=30),
    )
    lease = DeployLease(
        holder="gateway:pid1",
        held_for_s=30.0,
        expires_in_s=300.0,
        note=None,
        kind="rollout",
    )

    def _dead_pidfile(_path: str) -> tuple[bool, int | None]:
        return False, None

    def _no_sessions() -> tuple[list[SessionInfo], int, int]:
        return [], 0, 0

    def _no_log(_session: str) -> None:
        return None

    monkeypatch.setattr(cluster_status, "_check_pidfile", _dead_pidfile)
    monkeypatch.setattr(cluster_status, "_collect_sessions", _no_sessions)

    def _no_agents(_conn: object) -> int:
        return 0

    monkeypatch.setattr(cluster_status, "_count_local_agents", _no_agents)
    monkeypatch.setattr(cluster_status, "machine_name", lambda: "win")
    monkeypatch.setattr(cluster_status, "is_gateway", lambda: False)
    monkeypatch.setattr(cluster_status, "is_agent_runner", lambda: True)
    monkeypatch.setattr(cluster_status, "is_observability_station", lambda: False)
    monkeypatch.setattr("shared.cluster_drift.prod_source_head_sha", lambda: None)
    monkeypatch.setattr("shared.process_sha.get", lambda: None)
    monkeypatch.setattr("ops.updater_outcome._newest_log", _no_log)
    return state, lease


class _BatchOnlyBackend:
    def __init__(self, name: str):
        self.name = name
        self.batch_calls: list[list[str]] = []

    def list_sessions(self, prefix: str = "") -> list[str]:
        return [self.name] if self.name.startswith(prefix) else []

    def session_started_at(self, name: str) -> float | None:
        raise AssertionError(f"single timestamp read used for {name}")

    def session_started_ats(self, names: list[str]) -> dict[str, float | None]:
        self.batch_calls.append(names)
        return dict.fromkeys(names, 1000.0)


def test_collect_sessions_batches_timestamp_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each backend receives one timestamp batch, never one read per session."""
    service = _BatchOnlyBackend("ava-main-agent-host")
    shell = _BatchOnlyBackend("ava-main-agent-7-shell-0")
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: service)
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: shell)

    sessions, _, _ = cluster_status._collect_sessions()

    assert [session.name for session in sessions] == sorted([service.name, shell.name])
    assert service.batch_calls == [[service.name]]
    assert shell.batch_calls == [[shell.name]]


def test_collect_sessions_stamps_cluster_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Session created_at renders in the cluster timezone (user ruling
    2026-08-27), never the host OS zone — a runner whose OS zone differs must
    show the same wall clock as the gateway."""

    import datetime as dt
    from zoneinfo import ZoneInfo

    service = _BatchOnlyBackend("ava-main-agent-host")
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: service)
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: _BatchOnlyBackend("x"))
    from shared.config import settings
    from shared.config.general import GeneralSettings

    monkeypatch.setattr(
        settings, "general", GeneralSettings.model_construct(timezone="Asia/Shanghai")
    )

    sessions, _, _ = cluster_status._collect_sessions()
    created = sessions[0].created_at
    assert created is not None
    # epoch 1000 = 1970-01-01 00:16:40 UTC = 1970-01-01 08:16:40 +08:00
    assert created == dt.datetime(1970, 1, 1, 8, 16, 40, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_status_snapshot_uses_one_connection_while_sampling_resources(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_dependencies: tuple[HostDeployState, DeployLease],
) -> None:
    """One snapshot shares one connection while its one live sample runs in parallel."""
    state, lease = snapshot_dependencies
    conn = object()
    connect_calls = 0
    state_connections: list[object | None] = []
    lease_connections: list[object | None] = []
    sample_started = threading.Event()
    db_finished = threading.Event()
    sample_calls = 0

    @contextmanager
    def _connect(*, autocommit: bool = False) -> Generator[object, None, None]:
        nonlocal connect_calls
        connect_calls += 1
        assert autocommit is True
        assert sample_started.wait(timeout=2), "resource sampling did not overlap the DB read"
        try:
            yield conn
        finally:
            db_finished.set()

    def _read_state(_machine: str | None = None, *, conn: object | None = None) -> HostDeployState:
        state_connections.append(conn)
        return state

    def _read_lease(*, conn: object | None = None) -> DeployLease:
        lease_connections.append(conn)
        return lease

    def _sample() -> ResourceSample:
        nonlocal sample_calls
        sample_calls += 1
        sample_started.set()
        assert db_finished.wait(timeout=2), "DB reads did not overlap the resource sample"
        return _RESOURCE

    monkeypatch.setattr("shared.db.connect", _connect)
    monkeypatch.setattr("shared.host_deploy_state.read", _read_state)
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", _read_lease)
    monkeypatch.setattr("shared.resource_sample.resource_sample", _sample)

    snapshot = cluster_status.status_snapshot()

    assert connect_calls == 1
    assert state_connections == [conn]
    assert lease_connections == [conn]
    assert sample_calls == 1
    assert snapshot.paused is True
    assert snapshot.current_orchestration == "rollout"
    assert snapshot.resource == _RESOURCE


def test_status_snapshot_borrows_pool_once_with_a_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_dependencies: tuple[HostDeployState, DeployLease],
) -> None:
    """The ops daemon's pool contributes one bounded borrow, not fresh dials."""
    state, lease = snapshot_dependencies
    conn = object()
    pool = _Pool(conn)
    state_connections: list[object | None] = []
    lease_connections: list[object | None] = []

    def _read_state(_machine: str | None = None, *, conn: object | None = None) -> HostDeployState:
        state_connections.append(conn)
        return state

    def _read_lease(*, conn: object | None = None) -> DeployLease:
        lease_connections.append(conn)
        return lease

    def _fresh_connect(**_kwargs: object) -> object:
        raise AssertionError("pool-backed snapshot opened a fresh DB connection")

    monkeypatch.setattr("shared.db.connect", _fresh_connect)
    monkeypatch.setattr("shared.host_deploy_state.read", _read_state)
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", _read_lease)
    monkeypatch.setattr("shared.resource_sample.resource_sample", lambda: _RESOURCE)

    snapshot = cluster_status.status_snapshot(pool=pool)

    assert pool.timeouts == [2.0]
    assert state_connections == [conn]
    assert lease_connections == [conn]
    assert snapshot.paused is True


def test_two_status_snapshots_do_not_cache_db_or_resource_reads(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_dependencies: tuple[HostDeployState, DeployLease],
) -> None:
    """Sharing is snapshot-local: every later probe reads DB and resources again."""
    state, lease = snapshot_dependencies
    pool = _Pool(object())
    state_reads = 0
    lease_reads = 0
    sample_reads = 0

    def _read_state(_machine: str | None = None, *, conn: object | None = None) -> HostDeployState:
        nonlocal state_reads
        assert conn is pool.conn
        state_reads += 1
        return state

    def _read_lease(*, conn: object | None = None) -> DeployLease:
        nonlocal lease_reads
        assert conn is pool.conn
        lease_reads += 1
        return lease

    def _sample() -> ResourceSample:
        nonlocal sample_reads
        sample_reads += 1
        return _RESOURCE

    monkeypatch.setattr("shared.host_deploy_state.read", _read_state)
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", _read_lease)
    monkeypatch.setattr("shared.resource_sample.resource_sample", _sample)

    cluster_status.status_snapshot(pool=pool)
    cluster_status.status_snapshot(pool=pool)

    assert pool.timeouts == [2.0, 2.0]
    assert state_reads == 2
    assert lease_reads == 2
    assert sample_reads == 2


@pytest.mark.parametrize("stored_posture", ["idle", "paused"])
def test_status_snapshot_degrades_when_the_pool_cannot_reach_db(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_dependencies: tuple[HostDeployState, DeployLease],
    stored_posture: str,
) -> None:
    """DB-down is valid even if the unreachable row says the host was paused."""
    del snapshot_dependencies
    pool = _Pool(object(), error=RuntimeError(f"DB down with {stored_posture} row"))
    monkeypatch.setattr("shared.resource_sample.resource_sample", lambda: _RESOURCE)

    snapshot = cluster_status.status_snapshot(pool=pool)

    assert pool.timeouts == [2.0]
    assert snapshot.paused is False
    assert snapshot.current_orchestration is None
    assert snapshot.last_updater_outcome is None
    assert snapshot.agent_count == 0
    assert snapshot.resource == _RESOURCE


def test_resource_sample_failure_still_degrades_to_none_from_worker(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_dependencies: tuple[HostDeployState, DeployLease],
) -> None:
    """Moving the sample to a worker must not let its exception fail the probe."""
    state, lease = snapshot_dependencies
    pool = _Pool(object())

    def _read_state(_machine: str | None = None, *, conn: object | None = None) -> HostDeployState:
        del conn
        return state

    def _read_lease(*, conn: object | None = None) -> DeployLease:
        del conn
        return lease

    monkeypatch.setattr("shared.host_deploy_state.read", _read_state)
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", _read_lease)

    def _sample_failure() -> ResourceSample:
        raise RuntimeError("psutil unavailable")

    monkeypatch.setattr("shared.resource_sample.resource_sample", _sample_failure)

    snapshot = cluster_status.status_snapshot(pool=pool)

    assert snapshot.resource is None


def test_agent_count_reads_local_retained_identities_without_processes(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.db import create_agent

    monkeypatch.setattr(cluster_status, "machine_name", lambda: "count-host")
    for machine, status in (
        ("count-host", "running"),
        ("count-host", "idling"),
        ("count-host", "idling"),
        ("count-host", "terminated"),
        ("other-host", "idling"),
    ):
        agent = create_agent(db_conn)
        db_conn.execute(
            "INSERT INTO agents_meta(id,machine,status,runtime_kind,pid) "
            "VALUES(%s,%s,%s,'hosted',NULL)",
            (agent, machine, status),
        )
    db_conn.commit()
    assert cluster_status._count_local_agents(db_conn) == 3


def test_agent_count_uses_the_same_borrow_and_reaches_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_dependencies: tuple[HostDeployState, DeployLease],
) -> None:
    state, lease = snapshot_dependencies
    conn = object()
    pool = _Pool(conn)
    seen: list[object] = []

    def count(connection: object) -> int:
        seen.append(connection)
        return 7

    def read_state(**_kwargs: object) -> HostDeployState:
        return state

    def read_lease(**_kwargs: object) -> DeployLease:
        return lease

    monkeypatch.setattr(cluster_status, "_count_local_agents", count)
    monkeypatch.setattr("shared.host_deploy_state.read", read_state)
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", read_lease)
    monkeypatch.setattr(cluster_status, "_read_resource_sample", lambda: None)
    snapshot = cluster_status.status_snapshot(pool=pool)
    assert snapshot.agent_count == 7
    assert seen == [conn]
    assert pool.timeouts == [2.0]


@pytest.mark.parametrize("runner", [False, True])
def test_agent_host_liveness_is_probed_only_on_a_runner(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_dependencies: tuple[HostDeployState, DeployLease],
    runner: bool,
) -> None:
    from shared.config import settings

    probes: list[str] = []

    def check(path: str) -> tuple[bool, int]:
        probes.append(path)
        return True, 1234

    monkeypatch.setattr(cluster_status, "is_agent_runner", lambda: runner)
    monkeypatch.setattr(cluster_status, "_check_pidfile", check)

    def no_deploy(_pool: object) -> tuple[None, None, int]:
        return None, None, 0

    monkeypatch.setattr(cluster_status, "_read_deploy_snapshot", no_deploy)
    monkeypatch.setattr(cluster_status, "_read_resource_sample", lambda: None)
    snapshot = cluster_status.status_snapshot()
    assert snapshot.agent_host_online is (True if runner else None)
    assert (str(settings.services.agent_host_pidfile) in probes) is runner
    assert "restarter_online" not in snapshot.model_dump()
