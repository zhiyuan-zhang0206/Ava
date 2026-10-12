"""The page-server entry shares its captured image gate with logging and work."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot, field_domain, settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from services.agent_runner.page_server import daemon, resources
from tests.fixtures.configuration import snapshot_process_config


def _assert_gate_matches_image(
    gates: list[ProcessDbGate],
    reads: list[tuple[Path, str]],
    image: LoadedCommit,
    readers: list[Callable[[], str]],
    machine: MachineIdentity,
) -> None:
    assert len(gates) == 2 and gates[0] is gates[1]
    assert readers == [machine.host, machine.host]
    if image.sha is None:
        with pytest.raises(code_version.CodeVersionError, match="loaded commit"):
            gates[0].application_name()
        assert reads == []
    else:
        assert gates[0].application_name() == gates[1].application_name() == "ava:page_server:v42"
        assert reads == [(image.source_root, image.sha)]


def _run_main(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = asyncio.Runner()
    monkeypatch.setattr(daemon.asyncio, "Runner", lambda: runner)
    try:
        daemon.main()
    finally:
        runner.close()


@pytest.mark.parametrize("sha", ["captured-page-server", None])
def test_entry_shares_the_captured_gate_with_logging_health_and_work(
    monkeypatch: pytest.MonkeyPatch, sha: str | None
) -> None:
    from base.deploy.schema import migrations

    image = LoadedCommit(Path("/entry-page-server"), sha)
    stages: list[str] = []
    gates: list[ProcessDbGate] = []
    reads: list[tuple[Path, str]] = []
    factories: list[tuple[Callable[[], Database], MachineIdentity]] = []
    readers: list[Callable[[], str]] = []
    pipeline = object()

    class Store:
        def __init__(
            self, config: DbConfig, *, gate: ProcessDbGate, direct_url: ClusterDirectUrl
        ) -> None:
            gates.append(gate)
            readers.append(direct_url.local_host)

    def capture() -> LoadedCommit:
        stages.append("capture")
        return image

    def count(root: Path, commit: str) -> int:
        reads.append((root, commit))
        return 42

    def build(*, database: Callable[[], Database], machine: MachineIdentity, home: Path) -> object:
        stages.append("producer")
        assert gates == []
        factories.append((database, machine))
        return pipeline

    def initialize(**inputs: Any) -> None:
        stages.append("logging")
        assert inputs["name"] == "page_server"
        assert inputs["image"] is image
        assert inputs["producer"]() is pipeline
        assert inputs["machine_reader"]() == "page-server-entry"
        factories[0][0]()

    async def work(
        *,
        process_config: ConfigBoot,
        runtime: Path,
        database: Callable[[], Database],
        image: LoadedCommit,
        local_machine: MachineIdentity,
    ) -> None:
        stages.append("run")
        assert image is captured and database is factories[0][0]
        assert local_machine is factories[0][1]
        database()

    def check_schema(_url: str) -> None:
        stages.append("schema")

    def no_signals(_name: str) -> None:
        pass

    def exit_process(code: int) -> None:
        stages.append(f"exit:{code}")

    captured = image
    monkeypatch.setattr(settings.general, "machine_name", "page-server-entry")
    monkeypatch.setattr(daemon.LoadedCommit, "capture", capture)
    monkeypatch.setattr(code_version, "first_parent_count", count)
    monkeypatch.setattr(daemon, "Database", Store)
    monkeypatch.setattr(daemon, "ConfigBoot", snapshot_process_config)
    monkeypatch.setattr(daemon, "build_pipeline", build)
    monkeypatch.setattr(daemon, "init_gateway_process", initialize)
    monkeypatch.setattr(migrations, "assert_schema_current", check_schema)
    monkeypatch.setattr(daemon, "install_graceful_shutdown", no_signals)
    monkeypatch.setattr(daemon, "run", work)
    monkeypatch.setattr(daemon, "_hard_exit", exit_process)
    _run_main(monkeypatch)
    assert stages == ["capture", "schema", "producer", "logging", "run", "exit:0"]
    _assert_gate_matches_image(gates, reads, image, readers, factories[0][1])


def test_resources_follow_the_entry_owner_at_each_original_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = snapshot_process_config()
    runtime = tmp_path / "owned-entry" / "run"
    domain = field_domain("page_server_health_port")
    fields = getattr(config.view, domain)
    monkeypatch.setattr(fields, "page_server_health_port", 26101)
    monkeypatch.setenv("AVA_HOME", str(tmp_path / "a-later-home"))

    first = resources.endpoint(config, runtime=runtime)
    monkeypatch.setattr(fields, "page_server_health_port", 26102)
    second = resources.endpoint(config, runtime=runtime)

    assert (first.health_port, second.health_port) == (26101, 26102)
    assert first.pidfile == second.pidfile == runtime / "page_server.pid"
    assert runtime.is_dir()
    assert not (tmp_path / "a-later-home" / "run").exists()
