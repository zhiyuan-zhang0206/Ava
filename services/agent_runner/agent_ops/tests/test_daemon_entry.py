"""The executable owns one image, admission gate and bounded event writer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig, db_config_from_boot
from base.host.system.hostport import Host
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import DrainPhase, DrainResult, DrainStatus
from services.agent_runner.agent_ops import daemon


class _ExitError(Exception):
    def __init__(self, code: int) -> None:
        self.code = code


def _assert_run_owner(
    database: Callable[[], Database],
    image: LoadedCommit,
    captures: list[object],
    roots: list[tuple[Callable[[], Database], MachineIdentity]],
    gates: list[ProcessDbGate],
    readers: list[Callable[[], str]],
    machine: MachineIdentity,
) -> None:
    """The run borrows exactly the entry's image, database factory and identity."""
    assert image is captures[0]
    assert len(roots) == 1 and roots[0][0] is database
    assert roots[0][1] is machine
    assert database() is database()
    assert gates[0] is gates[1]
    assert readers == [machine.host, machine.host]


@pytest.mark.parametrize("sha", ["captured-image", None])
@pytest.mark.parametrize("failure", [None, RuntimeError("dispatch crash"), KeyboardInterrupt()])
def test_entry_shares_image_gate_factory_and_closes_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    process_config: ConfigBoot,
    sha: str | None,
    failure: BaseException | None,
) -> None:
    image = LoadedCommit(source_root=tmp_path, sha=sha)
    captures: list[object] = []
    gates: list[ProcessDbGate] = []
    events: list[object] = []
    roots: list[tuple[Callable[[], Database], MachineIdentity]] = []
    readers: list[Callable[[], str]] = []
    runner = asyncio.Runner()

    class Pipeline:
        def stop(self, timeout: float) -> DrainResult:
            events.append(("stop", timeout))
            return DrainResult(DrainStatus.COMPLETED, DrainPhase.STOP)

    pipeline = Pipeline()

    def capture() -> LoadedCommit:
        captures.append(image)
        return image

    def build_database(
        value: DbConfig, *, gate: ProcessDbGate, direct_url: ClusterDirectUrl
    ) -> Database:
        assert value == db_config_from_boot(process_config)
        gates.append(gate)
        readers.append(direct_url.local_host)
        return database

    def build_pipeline(
        *, database: Callable[[], Database], machine: MachineIdentity, home: Path
    ) -> EventPipeline:
        roots.append((database, machine))
        return cast(EventPipeline, pipeline)

    def init(**kwargs: Any) -> None:
        assert kwargs["name"] == "ops"
        assert kwargs["image"] is image
        assert kwargs["producer"]() is pipeline
        assert kwargs["producer"]() is pipeline
        assert kwargs["machine_reader"] == roots[0][1].name
        events.append("logging")

    async def run(
        *,
        database: Callable[[], Database],
        image: LoadedCommit,
        local_machine: MachineIdentity,
        config: ConfigBoot,
        host: Host,
    ) -> None:
        _assert_run_owner(database, image, captures, roots, gates, readers, local_machine)
        events.append("run")
        if failure is not None:
            raise failure

    def hard_exit(code: int) -> None:
        raise _ExitError(code)

    monkeypatch.setattr(daemon.LoadedCommit, "capture", capture)
    monkeypatch.setattr(daemon, "Database", build_database)
    monkeypatch.setattr(daemon, "ConfigBoot", lambda: process_config)
    monkeypatch.setattr(daemon, "build_pipeline", build_pipeline)
    monkeypatch.setattr(daemon, "init_gateway_process", init)

    def install_shutdown(_name: str) -> None:
        return None

    monkeypatch.setattr(daemon, "install_graceful_shutdown", install_shutdown)
    monkeypatch.setattr(daemon.asyncio, "Runner", lambda: runner)
    monkeypatch.setattr(daemon, "serve_ops_daemon", run)
    monkeypatch.setattr(daemon, "_hard_exit", hard_exit)
    try:
        with pytest.raises(_ExitError) as exited:
            daemon.main(argv=[])
        assert exited.value.code == (1 if isinstance(failure, Exception) else 0)
        assert captures == [image]
        assert events == ["logging", "run", ("stop", 2)]
    finally:
        runner.close()


def test_entry_keeps_startup_failure_when_writer_close_also_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(daemon, "ConfigBoot", snapshot_process_config)
    primary = RuntimeError("logging startup refused")
    closed: list[float] = []

    def init(**_kwargs: Any) -> None:
        raise primary

    def close(_self: object, *, pipeline_timeout: float) -> None:
        closed.append(pipeline_timeout)
        raise RuntimeError("writer stop refused")

    monkeypatch.setattr(
        daemon.LoadedCommit, "capture", lambda: LoadedCommit(source_root=tmp_path, sha=None)
    )
    monkeypatch.setattr(daemon, "ConfigBoot", snapshot_process_config)
    monkeypatch.setattr(daemon, "init_gateway_process", init)
    monkeypatch.setattr(daemon.ClientSet, "close", close)
    with pytest.raises(RuntimeError) as raised:
        daemon.main(argv=[])
    assert raised.value is primary
    assert closed == [2]
    assert raised.value.__notes__ == [
        "ops event pipeline shutdown failed: RuntimeError('writer stop refused')"
    ]
