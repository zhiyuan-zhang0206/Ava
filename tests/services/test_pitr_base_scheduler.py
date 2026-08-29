from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import psutil
import pytest

import services.pitr.base_scheduler_daemon as daemon
from services.pitr.base_manifest import BaseObject, CandidateManifest, WalRange
from services.pitr.base_scheduler_daemon import BaseCandidateState, _components, is_due
from services.pitr.retention_planner import DryRunResult
from services.pitr.retention_scheduler import RetentionDryRunState
from services.pitr.retention_scheduler import health_component as retention_health_component
from services.pitr.worker_process import (
    WorkerQueue,
    group_members,
    reap_restore_subprocess_group,
)
from shared.process_env import restricted_process_env


def _candidate(chain_id: str) -> CandidateManifest:
    return CandidateManifest(
        schema_version=1,
        chain_id=chain_id,
        protected=False,
        postgres_major=17,
        database_name="ava",
        system_identifier="1",
        wal_segment_size=16 * 1024 * 1024,
        timeline=1,
        start_lsn="0/100",
        end_lsn="0/200",
        wal_ranges=(WalRange(1, "0/100", "0/200"),),
        base_object=BaseObject("base", 1, 10, "crc", "sha", 5, "key", "AVAPITRB1"),
        native_manifest_sha256="manifest",
        native_manifest_member_path="backup_manifest",
        native_manifest_container_object_name="base",
        native_manifest_container_generation=1,
        migration_set_sha256="migrations",
    )


def test_restore_worker_exec_import_boundary_has_no_publisher_or_settings(tmp_path: Path) -> None:
    uploader = tmp_path / "uploader.json"
    uploader.write_text("publisher-only")
    inputs = daemon._RestoreWorkerInput(
        _candidate("viewer-only").to_json(),
        tmp_path,
        tmp_path / "ack",
        tmp_path / "backup.key",
        "project",
        "bucket",
        tmp_path / "viewer.json",
        daemon.RestoreSpaceBudget(0, 0, 0),
        "postgresql://viewer@127.0.0.1:5433/ava",
        Path("/usr/bin/true"),
        Path("/usr/bin/true"),
    )
    assert str(uploader) not in repr(inputs)
    environment = restricted_process_env()
    assert not any(name.startswith(("AVA_", "GOOGLE_", "PG")) for name in environment)
    script = (
        "import sys; import services.pitr.restore_worker; "
        "forbidden={'shared.config','services.pitr.restore_publish_store'}; "
        "raise SystemExit(1 if forbidden & set(sys.modules) else 0)"
    )
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        check=False,
    )
    assert completed.returncode == 0


def test_restricted_restore_group_reaps_orphan_descendant() -> None:
    script = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)']);"
        "time.sleep(60)"
    )
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", script], start_new_session=True, text=True
    )
    created_at = psutil.Process(process.pid).create_time()
    deadline = time.monotonic() + 10
    while len(group_members(process.pid)) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(group_members(process.pid)) >= 2

    reap_restore_subprocess_group(process, created_at)

    assert group_members(process.pid) == []


def test_authoritative_verify_precedes_publisher_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    constructed = False

    def reject_mismatched_ack(**_kwargs: object) -> None:
        raise ValueError("ACK generation mismatch")

    class Publisher:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal constructed
            constructed = True

    monkeypatch.setattr(daemon, "verify_candidate_proof", reject_mismatched_ack)
    monkeypatch.setattr(daemon, "GCSProtectedManifestPublisher", Publisher)

    with pytest.raises(ValueError, match="ACK generation mismatch"):
        daemon._verify_then_construct_publisher(
            candidate=_candidate("verify-first"),
            root=tmp_path,
            ack_dir=tmp_path / "ack",
            project="project",
            bucket="bucket",
            credentials=tmp_path / "uploader.json",
        )

    assert constructed is False


def _blocking_worker(
    started: Path,
    stopped: Path,
    stop: daemon.StopSignal,
    _output: WorkerQueue,
) -> None:
    started.write_text(str(os.getpid()))
    stop.wait()
    stopped.write_text("stopped")


def _noncooperative_worker(
    started: Path,
    armed: Path,
    late: Path,
    _stop: daemon.StopSignal,
    _output: WorkerQueue,
) -> None:
    script = (
        "import signal,subprocess,sys,time\n"
        f"late={str(late)!r}\n"
        "def spawn_late(*_args):\n"
        " p=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])\n"
        " open(late,'w').write(str(p.pid))\n"
        "signal.signal(signal.SIGTERM,spawn_late)\n"
        f"open({str(armed)!r},'w').write('armed')\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script])  # noqa: S603
    while not armed.exists():
        time.sleep(0.01)
    started.write_text(f"{os.getpid()} {child.pid}")
    time.sleep(60)


async def _wait_for_path(path: Path, *, timeout_s: float = 10) -> None:
    deadline = time.monotonic() + timeout_s
    while not path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert path.exists(), f"worker did not publish {path.name} before the deadline"


async def _assert_tree_gone(pids: list[int], timeout_s: float = 5.0) -> None:
    # After SIGKILL, dead processes can remain zombies until their new parent
    # reaps them, and psutil.pid_exists() still reports those entries. Assert
    # no live members, not that every process-table entry vanished (same
    # discipline as tests/agent/test_exec_subprocess.py::_assert_tree_gone).
    deadline = time.monotonic() + timeout_s
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        for pid in list(remaining):
            try:
                if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                    remaining.discard(pid)
            except psutil.NoSuchProcess:
                remaining.discard(pid)
        if remaining:
            await asyncio.sleep(0.05)
    assert not remaining, f"process(es) still alive after forced shutdown: {sorted(remaining)}"


def test_due_uses_durable_candidate_after_restart(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, 4, tzinfo=UTC)  # Sunday after the weekly window.
    assert is_due(now, tmp_path)
    (tmp_path / "20260830T030000Z.candidate.json").write_text(
        _candidate("20260830T030000Z").to_json()
    )
    assert not is_due(now, tmp_path)


def test_activation_candidate_never_satisfies_weekly_due(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, 4, tzinfo=UTC)
    chain = "activation-20260830T030000Z-00000000-0000-0000-0000-000000000001"
    (tmp_path / f"{chain}.candidate.json").write_text(_candidate(chain).to_json())
    assert is_due(now, tmp_path)


def test_health_surfaces_corrupt_activation_state_without_throwing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operation = tmp_path / "physical-backup" / "activation" / "operation.json"
    operation.parent.mkdir(parents=True)
    operation.write_text("{corrupt")
    monkeypatch.setattr("services.pitr.activation_runtime.ava_home", lambda: tmp_path)
    components = daemon._components(BaseCandidateState())
    activation_component = next(item for item in components if item["name"] == "pitr_activation")
    assert activation_component["status"] == "degraded"
    assert activation_component["progress"] == "unknown"
    assert activation_component["gate_readiness"] is False


def test_health_never_calls_a_candidate_protected() -> None:
    components = _components(BaseCandidateState(running=True))
    assert components[0]["protected"] is False


def test_retention_health_is_explicitly_dry_run_only(tmp_path: Path) -> None:
    result = DryRunResult(tmp_path / "plan", "digest", False, 3, 2, 30, 20)
    components = daemon._components(
        BaseCandidateState(
            retention=RetentionDryRunState(
                enabled=True, plan=result, last_attempt=time.time(), last_success=time.time()
            )
        )
    )
    retention = next(item for item in components if item["name"] == "pitr_retention_dry_run")
    assert retention["delete_enabled"] is False
    assert retention["eligible_objects"] == 2
    assert retention["eligible_bytes"] == 20


def test_retention_health_never_exposes_stale_or_failed_eligibility(tmp_path: Path) -> None:
    result = DryRunResult(tmp_path / "plan", "digest", False, 3, 2, 30, 20)
    state = RetentionDryRunState(
        enabled=True,
        plan=result,
        last_attempt=time.time(),
        last_success=time.time(),
        last_error="inventory unavailable",
    )
    retention = retention_health_component(state)
    assert retention["status"] == "degraded"
    assert retention["eligible_objects"] == 0
    assert retention["eligible_bytes"] == 0

    state.last_error = None
    state.last_success = time.time() - 3 * 3600
    stale = retention_health_component(state)
    assert stale["progress"] == "stale"
    assert stale["current"] is False
    assert stale["plan_digest"] is None

    state.enabled = False
    disabled = retention_health_component(state)
    assert disabled["progress"] == "disabled"
    assert disabled["current"] is False
    assert disabled["retained_objects"] == 0
    assert disabled["eligible_objects"] == 0
    assert disabled["retained_bytes"] == 0
    assert disabled["eligible_bytes"] == 0


@pytest.mark.asyncio
async def test_runner_cancellation_reaps_active_worker(
    tmp_path: Path,
) -> None:
    started = tmp_path / "started"
    stopped = tmp_path / "stopped"

    task = asyncio.create_task(
        daemon._run_worker(target=partial(_blocking_worker, started, stopped))
    )
    await _wait_for_path(started)
    child_pid = int(started.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stopped.read_text() == "stopped"
    await _assert_tree_gone([child_pid])


# ── QA #931 R3: domain conditions never gate readiness ────────────────────


async def _http_get_status(port: int) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    return int(status_line.split(" ")[1]), body


def _find_free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_degraded_domain_condition_keeps_healthz_200() -> None:
    """QA #931 R3 discriminator: a domain condition (cleanup pending, last
    error, stale candidate) must NOT flip /healthz to 503 — a respawn cannot
    fix it, so the watchdog would restart-flap a healthy daemon every 60s.
    The component reports degraded with gate_readiness=False; readiness
    follows process liveness only."""
    from shared.daemon_health import Liveness, start_health_server, stop_health_server

    state = BaseCandidateState(last_error="GCS credentials rejected")
    port = _find_free_port()
    liveness = Liveness(timeout_s=120)
    server = await start_health_server(
        "pitr_base_backup",
        port=port,
        liveness=liveness,
        components=lambda: _components(state),
    )
    try:
        status, body = await _http_get_status(port)
        assert status == 200, "domain condition must not gate readiness"
        payload = json.loads(body)
        assert payload["readiness"] == "ok"
        comp = next(c for c in payload["components"] if c["name"] == "pitr_base_candidate")
        assert comp["status"] == "degraded"
        assert comp["gate_readiness"] is False
        assert "GCS credentials rejected" in comp["detail"]
        # The liveness lane still gates: a dead/wedged daemon flips to 503.
        liveness._last = time.monotonic() - 1000
        status, _ = await _http_get_status(port)
        assert status == 503, "wedged daemon (stale liveness) still gates readiness"
    finally:
        await stop_health_server(server)


@pytest.mark.asyncio
async def test_forced_shutdown_reaps_noncooperative_group_and_late_fork(
    tmp_path: Path,
) -> None:
    started = tmp_path / "started"
    armed = tmp_path / "armed"
    late = tmp_path / "late"
    task = asyncio.create_task(
        daemon._run_worker(
            target=partial(_noncooperative_worker, started, armed, late),
            cooperative_timeout_s=0.1,
            group_grace_s=3,
            group_deadline_s=15,
        )
    )
    await _wait_for_path(started)
    worker_pid, child_pid = (int(value) for value in started.read_text().split())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _wait_for_path(late)
    late_pid = int(late.read_text())
    await _assert_tree_gone([worker_pid, child_pid, late_pid])
