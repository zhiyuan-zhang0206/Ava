"""The heartbeat service's loop structure: the TaskGroup that owns the three loops,
a crash that ends the process, per-loop progress, and the completion digest as a
loop of the service rather than of the gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.daemon.loop_health import LivenessGroup, LoopProgress
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.tests.fakes import patch_database
from base.events.live.bus import EventBus
from base.native_process.loaded_commit import LoadedCommit
from services.wake.heartbeat import completion_digest, daemon
from tests.fixtures.unit.identity import machine_identity as machine_identity


def _patch_run(
    monkeypatch: pytest.MonkeyPatch,
    loops: dict[str, Callable[..., Any]],
    events: list[str],
    seen: dict[str, object],
    *,
    machine_identity: MachineIdentity,
) -> None:
    class _Pool:
        def close(self) -> None:
            events.append("pool")

    async def fake_start(
        _name: str, _port: int, *, liveness: LivenessGroup, image: LoadedCommit
    ) -> object:
        seen["image"] = image
        seen["trackers"] = sorted(liveness.snapshot())
        return object()

    async def fake_stop(_server: object) -> None:
        events.append("health")

    def is_running_for_entry(config: ConfigBoot, *, runtime: Path) -> bool:
        return False

    monkeypatch.setattr(daemon, "_is_running", is_running_for_entry)

    def write_pidfile_for_entry(config: ConfigBoot, *, runtime: Path) -> None:
        pass

    monkeypatch.setattr(daemon, "_write_pidfile", write_pidfile_for_entry)

    def remove_pidfile_for_entry(config: ConfigBoot, *, runtime: Path) -> None:
        return events.append("pidfile")

    monkeypatch.setattr(daemon, "_remove_pidfile", remove_pidfile_for_entry)
    monkeypatch.setattr(daemon, "start_health_server", fake_start)
    monkeypatch.setattr(daemon, "stop_health_server", fake_stop)
    patch_database(monkeypatch, pool=_Pool)
    monkeypatch.setattr(daemon, "_dispatch_loop", loops["dispatch"])
    monkeypatch.setattr(daemon, "_liveness_loop", loops["liveness"])

    async def digest_loop(
        pool: ConnectionPool,
        db: Database,
        bus: EventBus,
        progress: LoopProgress,
        *,
        machine: MachineIdentity,
    ) -> None:
        assert machine is machine_identity
        await loops["digest"](pool, db, bus, progress)

    monkeypatch.setattr(daemon.completion_digest, "completion_digest_loop", digest_loop)


def test_every_loop_gets_its_own_progress_tracker(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    *,
    process_config: ConfigBoot,
    tmp_path: Path,
    machine_identity: MachineIdentity,
) -> None:
    received: dict[str, LoopProgress] = {}

    def loop(name: str) -> Callable[..., Any]:
        async def run_loop(*args: object) -> None:
            received[name] = cast(LoopProgress, args[-1])

        return run_loop

    seen: dict[str, object] = {}
    _patch_run(
        monkeypatch,
        {"dispatch": loop("dispatch"), "liveness": loop("liveness"), "digest": loop("digest")},
        [],
        seen,
        machine_identity=machine_identity,
    )

    asyncio.run(
        asyncio.wait_for(
            daemon.run(
                process_config=process_config,
                runtime=tmp_path / "run",
                database=lambda: database,
                image=LoadedCommit(Path(), None),
                machine=machine_identity,
            ),
            timeout=5.0,
        )
    )

    assert seen["image"] == LoadedCommit(Path(), None)
    assert seen["trackers"] == ["completion_digest", "dispatch", "liveness"]
    assert len({id(progress) for progress in received.values()}) == 3


@pytest.mark.parametrize("crashing", ["dispatch", "liveness", "digest"])
def test_a_crashing_loop_cancels_its_siblings_and_ends_the_service(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    crashing: str,
    *,
    process_config: ConfigBoot,
    tmp_path: Path,
    machine_identity: MachineIdentity,
) -> None:
    cancelled: list[str] = []
    events: list[str] = []

    def loop(name: str) -> Callable[..., Any]:
        async def run_loop(*_args: object) -> None:
            if name == crashing:
                await asyncio.sleep(0.01)
                raise RuntimeError(f"{name} crashed")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(name)
                raise

        return run_loop

    _patch_run(
        monkeypatch,
        {"dispatch": loop("dispatch"), "liveness": loop("liveness"), "digest": loop("digest")},
        events,
        {},
        machine_identity=machine_identity,
    )

    with pytest.raises(ExceptionGroup) as raised:
        asyncio.run(
            asyncio.wait_for(
                daemon.run(
                    process_config=process_config,
                    runtime=tmp_path / "run",
                    database=lambda: database,
                    image=LoadedCommit(Path(), None),
                    machine=machine_identity,
                ),
                timeout=5.0,
            )
        )

    assert [str(exc) for exc in raised.value.exceptions] == [f"{crashing} crashed"]
    assert sorted(cancelled) == sorted({"dispatch", "liveness", "digest"} - {crashing})
    assert sorted(events) == ["health", "pidfile", "pool"]


async def test_the_digest_loop_flushes_at_once_then_paces(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    flushed: list[object] = []

    async def flush_once(
        pool: object,
        _db: object,
        _bus: object,
        *,
        now: datetime | None = None,
        machine: MachineIdentity,
    ) -> int:
        flushed.append(pool)
        return 0

    monkeypatch.setattr(completion_digest, "flush_once", flush_once)
    pool = cast(ConnectionPool, object())
    task = asyncio.create_task(
        completion_digest.completion_digest_loop(
            pool,
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            LoopProgress("digest", 300.0),
            machine=machine_identity,
        )
    )
    try:
        for _ in range(200):
            if flushed:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert flushed == [pool]  # a flush at once; the 60 s wait never elapses


async def test_a_failing_flush_ends_the_digest_loop(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    async def flush_once(
        pool: object,
        _db: object,
        _bus: object,
        *,
        now: datetime | None = None,
        machine: MachineIdentity,
    ) -> int:
        raise RuntimeError("digest table unreadable")

    monkeypatch.setattr(completion_digest, "flush_once", flush_once)

    with pytest.raises(RuntimeError, match="digest table unreadable"):
        await completion_digest.completion_digest_loop(
            cast(ConnectionPool, object()),
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            LoopProgress("digest", 300.0),
            machine=machine_identity,
        )
