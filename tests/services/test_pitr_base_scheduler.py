from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import psutil
import pytest

import services.pitr.base_scheduler_daemon as daemon
from services.pitr.base_manifest import BaseObject, CandidateManifest, WalRange
from services.pitr.base_scheduler_daemon import BaseCandidateState, _components, is_due


def _candidate(chain_id: str) -> CandidateManifest:
    return CandidateManifest(
        schema_version=1,
        chain_id=chain_id,
        protected=False,
        postgres_major=17,
        system_identifier="1",
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


def _blocking_worker(
    started: Path,
    stopped: Path,
    stop: daemon.StopSignal,
    _output: daemon._WorkerQueue,
) -> None:
    started.write_text(str(os.getpid()))
    stop.wait()
    stopped.write_text("stopped")


def test_due_uses_durable_candidate_after_restart(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, 4, tzinfo=UTC)  # Sunday after the weekly window.
    assert is_due(now, tmp_path)
    (tmp_path / "20260830T030000Z.candidate.json").write_text(
        _candidate("20260830T030000Z").to_json()
    )
    assert not is_due(now, tmp_path)


def test_health_never_calls_a_candidate_protected() -> None:
    components = _components(BaseCandidateState(running=True))
    assert components[0]["protected"] is False


@pytest.mark.asyncio
async def test_runner_cancellation_reaps_active_worker(
    tmp_path: Path,
) -> None:
    started = tmp_path / "started"
    stopped = tmp_path / "stopped"

    task = asyncio.create_task(
        daemon._run_worker(target=partial(_blocking_worker, started, stopped))
    )
    for _ in range(100):
        if started.exists():
            break
        await asyncio.sleep(0.01)
    child_pid = int(started.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stopped.read_text() == "stopped"
    assert not psutil.pid_exists(child_pid)


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
