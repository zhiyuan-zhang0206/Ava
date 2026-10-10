"""The computer entry shares one captured image gate with logging and actions."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from services.desktop.computer import mcp_daemon as daemon


@pytest.mark.parametrize("sha", ["captured-computer", None])
def test_entry_shares_the_captured_gate_with_logging_and_actions(
    monkeypatch: pytest.MonkeyPatch, sha: str | None
) -> None:
    image = LoadedCommit(Path("/entry-computer"), sha)
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
        assert gates == []  # The producer keeps the existing lazy database dial.
        factories.append(database)
        return pipeline

    def initialize(**inputs: Any) -> None:
        stages.append("logging")
        assert inputs["name"] == "computer-mcp"
        assert inputs["image"] is image
        assert inputs["producer"]() is pipeline
        assert inputs["machine_reader"]() == "computer-entry"
        factories[0]()

    async def work(sock: str | None = None, *, database: Callable[[], Database]) -> None:
        stages.append("run")
        assert sock is None
        assert database is factories[0]
        database()

    monkeypatch.setattr(settings.general, "machine_name", "computer-entry")
    monkeypatch.setattr(daemon.LoadedCommit, "capture", capture)
    monkeypatch.setattr(code_version, "first_parent_count", count)
    monkeypatch.setattr(daemon, "Database", Store)
    monkeypatch.setattr(daemon, "build_pipeline", build)
    monkeypatch.setattr(daemon, "init_gateway_process", initialize)
    monkeypatch.setattr(daemon, "run", work)
    daemon.main()
    assert stages == ["capture", "producer", "logging", "run"]
    assert len(gates) == 2 and gates[0] is gates[1]
    if sha is None:
        with pytest.raises(code_version.CodeVersionError, match="loaded commit"):
            gates[0].application_name()
        assert reads == []
    else:
        assert gates[0].application_name() == gates[1].application_name() == "ava:computer-mcp:v42"
        assert reads == [(image.source_root, sha)]
