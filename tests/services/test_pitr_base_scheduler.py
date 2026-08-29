from __future__ import annotations

import asyncio
import os
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
