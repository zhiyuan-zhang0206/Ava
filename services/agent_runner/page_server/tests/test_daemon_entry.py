"""The page-server entry shares its captured image gate with logging and work."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from services.agent_runner.page_server import daemon


def _assert_gate_matches_image(
    gates: list[ProcessDbGate], reads: list[tuple[Path, str]], image: LoadedCommit
) -> None:
    assert len(gates) == 2 and gates[0] is gates[1]
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
        assert inputs["name"] == "page_server"
        assert inputs["image"] is image
        assert inputs["producer"]() is pipeline
        assert inputs["machine_reader"]() == "page-server-entry"
        factories[0]()

    async def work(*, database: Callable[[], Database], image: LoadedCommit) -> None:
        stages.append("run")
        assert image is captured and database is factories[0]
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
    monkeypatch.setattr(daemon, "build_pipeline", build)
    monkeypatch.setattr(daemon, "init_gateway_process", initialize)
    monkeypatch.setattr(migrations, "assert_schema_current", check_schema)
    monkeypatch.setattr(daemon, "install_graceful_shutdown", no_signals)
    monkeypatch.setattr(daemon, "run", work)
    monkeypatch.setattr(daemon, "_hard_exit", exit_process)
    _run_main(monkeypatch)
    assert stages == ["capture", "schema", "producer", "logging", "run", "exit:0"]
    _assert_gate_matches_image(gates, reads, image)
