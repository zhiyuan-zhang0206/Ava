"""Task maintenance binds its captured image before logging, health and work."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from psycopg_pool import ConnectionPool

from ava_builtins.plugins.ava_fleet.default_config import FleetConfig
from ava_builtins.plugins.ava_fleet.task_maintenance import daemon
from base.config import settings
from base.daemon.endpoints import ServiceEndpoint
from base.daemon.health import Liveness
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit


def _assert_gate_matches_image(
    gates: list[ProcessDbGate], reads: list[tuple[Path, str]], image: LoadedCommit
) -> None:
    assert len(gates) == 2 and gates[0] is gates[1]
    if image.sha is None:
        with pytest.raises(code_version.CodeVersionError, match="loaded commit"):
            gates[0].application_name()
        assert reads == []
    else:
        assert (
            gates[0].application_name() == gates[1].application_name() == "ava:task_maintenance:v42"
        )
        assert reads == [(image.source_root, image.sha)]


def _patch_fleet_config(
    monkeypatch: pytest.MonkeyPatch, config: FleetConfig, delivered: bool
) -> None:
    def service(name: str, model: type[FleetConfig]) -> FleetConfig | None:
        assert name == "ava_fleet" and model is FleetConfig
        return config if delivered else None

    def authority(name: str, model: type[FleetConfig], path: Path) -> FleetConfig:
        assert (name, model, path) == ("ava_fleet", FleetConfig, Path("/fleet-image.json"))
        return config

    def image_path(_name: str) -> Path:
        return Path("/fleet-image.json")

    monkeypatch.setattr(daemon, "read_service_config", service)
    monkeypatch.setattr(daemon, "read_authority_config", authority)
    monkeypatch.setattr(daemon, "disk_image_path", image_path)


@pytest.mark.parametrize("sha", ["captured-task-maintenance", None])
def test_entry_shares_the_image_gate_and_fleet_config(
    monkeypatch: pytest.MonkeyPatch, sha: str | None
) -> None:
    from base.deploy.schema import migrations

    image = LoadedCommit(Path("/entry-task-maintenance"), sha)
    config = FleetConfig()
    stages: list[str] = []
    gates: list[ProcessDbGate] = []
    reads: list[tuple[Path, str]] = []
    factories: list[Callable[[], Database]] = []
    pipeline = object()

    class Store:
        @classmethod
        def from_settings(cls, *, gate: ProcessDbGate) -> Store:
            gates.append(gate)
            return cls()

    def capture() -> LoadedCommit:
        stages.append("capture")
        return image

    def count(root: Path, commit: str) -> int:
        reads.append((root, commit))
        return 42

    def build(*, database: Callable[[], Database]) -> object:
        stages.append("producer")
        assert gates == []
        factories.append(database)
        return pipeline

    def initialize(**inputs: Any) -> None:
        stages.append("logging")
        assert inputs["name"] == "task_maintenance"
        assert inputs["image"] is image
        assert inputs["producer"]() is pipeline
        assert inputs["machine_reader"]() == "fleet-entry"
        factories[0]()

    async def work(
        fleet: FleetConfig, *, database: Callable[[], Database], image: LoadedCommit
    ) -> None:
        stages.append("run")
        assert fleet is config and image is captured
        assert database is factories[0]
        database()

    def check_schema(_url: str) -> None:
        stages.append("schema")

    def no_signals(_name: str) -> None:
        pass

    captured = image
    monkeypatch.setattr(settings.general, "machine_name", "fleet-entry")
    monkeypatch.setattr(daemon.LoadedCommit, "capture", capture)
    monkeypatch.setattr(code_version, "first_parent_count", count)
    monkeypatch.setattr(daemon, "Database", Store)
    monkeypatch.setattr(daemon.telemetry, "build_pipeline", build)
    monkeypatch.setattr(daemon, "init_gateway_process", initialize)
    monkeypatch.setattr(migrations, "assert_schema_current", check_schema)
    monkeypatch.setattr(daemon, "install_graceful_shutdown", no_signals)
    _patch_fleet_config(monkeypatch, config, sha is not None)
    monkeypatch.setattr(daemon, "run", work)
    monkeypatch.setattr(daemon, "_remove_pidfile", lambda: stages.append("pidfile"))
    daemon.main()
    assert stages == ["capture", "schema", "producer", "logging", "run", "pidfile"]
    _assert_gate_matches_image(gates, reads, image)


@pytest.mark.parametrize("failure", [None, TypeError("invalid dispatch")])
async def test_run_keeps_health_before_pool_and_closes_after_dispatch(
    monkeypatch: pytest.MonkeyPatch, failure: Exception | None
) -> None:
    image = LoadedCommit(Path("/task-maintenance-work"), None)
    config = FleetConfig()
    bus = cast(EventBus, object())
    stages: list[str] = []

    class Pool:
        def close(self) -> None:
            stages.append("pool-close")

    pool = cast(ConnectionPool, Pool())

    class Store:
        def pool(self) -> ConnectionPool:
            stages.append("pool")
            return pool

    db = cast(Database, Store())

    def database() -> Database:
        stages.append("database")
        return db

    async def start(_name: str, _port: int, *, liveness: Liveness, image: LoadedCommit) -> object:
        assert image is captured and liveness is not None
        stages.append("health")
        return object()

    async def stop(_health: object) -> None:
        stages.append("health-close")

    async def dispatch(
        received_pool: ConnectionPool,
        received_db: Database,
        received_bus: EventBus,
        _liveness: Liveness,
        *,
        config: FleetConfig,
    ) -> None:
        assert (received_pool, received_db, received_bus) == (pool, db, bus)
        assert config is fleet
        stages.append("dispatch")
        if failure is not None:
            raise failure

    captured, fleet = image, config
    monkeypatch.setattr(daemon, "_is_running", lambda: False)
    monkeypatch.setattr(daemon, "_write_pidfile", lambda: stages.append("pidfile"))
    monkeypatch.setattr(daemon, "_remove_pidfile", lambda: stages.append("pidfile-close"))
    monkeypatch.setattr(
        daemon, "_endpoint", lambda: ServiceEndpoint("task_maintenance", 1, Path("/pid"))
    )
    monkeypatch.setattr(daemon, "start_health_server", start)
    monkeypatch.setattr(daemon, "stop_health_server", stop)
    monkeypatch.setattr(daemon.EventBus, "from_settings", lambda: bus)
    monkeypatch.setattr(daemon, "_dispatch_loop", dispatch)
    if failure is None:
        await daemon.run(config, database=database, image=image)
    else:
        with pytest.raises(TypeError, match="invalid dispatch") as raised:
            await daemon.run(config, database=database, image=image)
        assert raised.value is failure
    assert stages == [
        "pidfile",
        "health",
        "database",
        "pool",
        "dispatch",
        "pool-close",
        "health-close",
        "pidfile-close",
    ]
