"""The shared browser entry binds one image and its explicit logging database."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from services.desktop.browser import mcp_daemon as daemon


def _assert_gate_matches_image(
    gates: list[ProcessDbGate], reads: list[tuple[Path, str]], image: LoadedCommit
) -> None:
    assert len(gates) == 2 and gates[0] is gates[1]
    if image.sha is None:
        with pytest.raises(code_version.CodeVersionError, match="loaded commit"):
            gates[0].application_name()
        assert reads == []
    else:
        assert gates[0].application_name() == gates[1].application_name() == "ava:browser-mcp:v42"
        assert reads == [(image.source_root, image.sha)]


@pytest.mark.parametrize("sha", ["captured-browser", None])
def test_entry_keeps_the_config_owner_and_captured_image(
    monkeypatch: pytest.MonkeyPatch, sha: str | None
) -> None:
    boot = ConfigBoot()
    boot.read_process_environment()
    boot.set_field("machine_name", "browser-entry")
    image = LoadedCommit(Path("/entry-browser"), sha)
    stages: list[str] = []
    gates: list[ProcessDbGate] = []
    slices: list[DbConfig] = []
    reads: list[tuple[Path, str]] = []
    factories: list[Callable[[], Database]] = []
    pipeline = object()

    class Store:
        def __init__(self, config: DbConfig, *, gate: ProcessDbGate) -> None:
            slices.append(config)
            gates.append(gate)

    def capture() -> LoadedCommit:
        stages.append("capture")
        return image

    def count(root: Path, commit: str) -> int:
        reads.append((root, commit))
        return 42

    def boot_owner(self: ConfigBoot) -> None:
        assert self is boot
        stages.append("boot")

    def build(*, database: Callable[[], Database]) -> object:
        stages.append("producer")
        assert gates == []  # Pipeline construction must not dial or resolve the image.
        factories.append(database)
        return pipeline

    def initialize(**inputs: Any) -> None:
        stages.append("logging")
        assert inputs["name"] == "browser-mcp"
        assert inputs["image"] is image
        assert inputs["producer"]() is pipeline
        assert inputs["machine_reader"]() == "browser-entry"
        factories[0]()

    async def work(*, browser_cdp_port: int, connect_timeout_reader: Callable[[], float]) -> None:
        stages.append("run")
        assert browser_cdp_port == boot.view.services.browser_cdp_port
        boot.set_field("mcp_connect_timeout_seconds", 17.0)
        assert connect_timeout_reader() == 17.0
        boot.set_field("db_pool_max_size", 37)
        factories[0]()

    monkeypatch.setattr(daemon.LoadedCommit, "capture", capture)
    monkeypatch.setattr(code_version, "first_parent_count", count)
    monkeypatch.setattr(daemon, "ConfigBoot", lambda: boot)
    monkeypatch.setattr(ConfigBoot, "boot", boot_owner)
    monkeypatch.setattr(daemon, "Database", Store)
    monkeypatch.setattr(daemon, "build_pipeline", build)
    monkeypatch.setattr(daemon, "init_gateway_process", initialize)
    monkeypatch.setattr(daemon, "run", work)
    daemon.main()
    assert stages == ["capture", "boot", "producer", "logging", "run"]
    assert slices[1].db_pool_max_size == 37
    _assert_gate_matches_image(gates, reads, image)
