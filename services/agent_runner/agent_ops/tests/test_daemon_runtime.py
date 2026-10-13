"""The ops serving loop retains its explicit config and resource owners."""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import snapshot_process_config
from base.daemon.endpoints import ServiceEndpoints
from base.daemon.loop_health import LivenessGroup
from base.db import Database
from base.native_process.loaded_commit import LoadedCommit
from services.agent_runner.agent_ops import boot, daemon


def _assert_live_saturation(inputs: dict[str, object], config: ConfigBoot) -> None:
    """The actual health callback rereads this root rather than a startup scalar."""
    routes = cast(dict[tuple[str, str], functools.partial[object]], inputs["extra_routes"])
    active = cast(dict[str, tuple[str, float]], routes[("POST", "/ops")].keywords["active_ops"])
    extra = cast(Callable[[], dict[str, object]], inputs["extra"])
    initial = config.view.services.ops_concurrency
    try:
        active["owned-test"] = ("status", 0.0)
        config.set_field("ops_concurrency", 2)
        assert extra()["saturation"] == 0.5
        config.set_field("ops_concurrency", 4)
        assert extra()["saturation"] == 0.25
    finally:
        active.clear()
        config.set_field("ops_concurrency", initial)


class _PidfileOperations:
    """Record cleanup while checking each resource operation's explicit owner."""

    def __init__(self, config: ConfigBoot, runtime: Path, events: list[str]) -> None:
        self.config = config
        self.runtime = runtime
        self.events = events

    def assert_owner(self, config: ConfigBoot, runtime: Path) -> None:
        assert config is self.config
        assert runtime == self.runtime

    def is_running(self, config: ConfigBoot, *, runtime: Path) -> bool:
        self.assert_owner(config, runtime)
        return False

    def write_pidfile(self, config: ConfigBoot, *, runtime: Path) -> None:
        self.assert_owner(config, runtime)

    def remove_pidfile(self, config: ConfigBoot, *, runtime: Path) -> None:
        self.assert_owner(config, runtime)
        self.events.append("pidfile")


async def test_server_failure_releases_owned_resources(
    monkeypatch: pytest.MonkeyPatch,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    unit_home: Path,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """The single serving loop keeps the primary failure and closes its resources."""
    from base.deploy.schema import migrations

    config = snapshot_process_config()
    events: list[str] = []

    class _Pool:
        def close(self) -> None:
            events.append("pool")

    class _Server:
        async def __aenter__(self) -> _Server:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def serve_forever(self) -> None:
            raise RuntimeError("ops server crashed")

    async def start_health(name: str, port: int, **kw: object) -> _Server:
        assert kw["image"] is ops_image
        _assert_live_saturation(kw, config)
        assert isinstance(kw["liveness"], LivenessGroup)
        assert kw["liveness"].snapshot() == {}
        return _Server()

    async def stop_health(_server: object) -> None:
        events.append("health")

    pidfiles = _PidfileOperations(config, unit_home / "run", events)
    monkeypatch.setattr(boot, "is_running", pidfiles.is_running)
    monkeypatch.setattr(boot, "write_pidfile", pidfiles.write_pidfile)
    monkeypatch.setattr(boot, "remove_pidfile", pidfiles.remove_pidfile)

    def schema_current(_url: str) -> None:
        return None

    monkeypatch.setattr(migrations, "assert_schema_current", schema_current)

    def open_pool(*, database: Callable[[], Database], authority: ConfigAuthority) -> _Pool:
        assert database is ops_database
        assert authority.runtime is config.ensure_eager()
        assert authority.env_path == unit_home / ".env"
        return _Pool()

    monkeypatch.setattr(boot, "open_db_pool", open_pool)
    monkeypatch.setattr(boot, "ops_acceptance", lambda: frozenset({"test-digest"}))

    def bind_host(*, machine: object) -> str:
        assert machine is machine_identity
        return "127.0.0.1"

    monkeypatch.setattr(boot, "ops_bind_host", bind_host)
    monkeypatch.setattr(daemon, "start_health_server", start_health)
    monkeypatch.setattr(daemon, "stop_health_server", stop_health)

    def register_boot(
        *,
        database: Callable[[], Database],
        config: ConfigBoot,
        machine: MachineIdentity,
        ops_port: int,
        home: Path,
    ) -> None:
        assert database is ops_database
        assert machine is machine_identity
        assert (
            ops_port
            == ServiceEndpoints.from_boot(config, runtime=unit_home / "run").of("ops").health_port
        )
        assert home == unit_home

    monkeypatch.setattr(boot, "register_boot", register_boot)
    assert (
        ServiceEndpoints.from_boot(config, runtime=unit_home / "run").of("ops").health_port
    )  # the port table still names the ops slot

    with pytest.raises(RuntimeError, match="ops server crashed"):
        await daemon._main(
            database=ops_database,
            image=ops_image,
            local_machine=machine_identity,
            config=config,
        )

    assert sorted(events) == ["health", "pidfile", "pool"]
