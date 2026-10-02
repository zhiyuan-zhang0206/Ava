"""`services.milvus.daemon.main` takes its data directory and port from the settings when it runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from base.config import settings
from services.milvus import daemon


def test_main_execs_milvus_lite_with_the_configured_dir_and_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    data_dir = tmp_path / "milvus-here"
    monkeypatch.setattr(settings.services, "milvus_data_dir", data_dir)
    monkeypatch.setattr(settings.services, "milvus_port", 19777)

    def fake_dup2(*_args: int) -> int:
        return 0

    monkeypatch.setattr(daemon.os, "dup2", fake_dup2)
    exec_calls: list[tuple[str, list[str]]] = []

    def fake_execvp(file: str, args: list[str]) -> Any:
        exec_calls.append((file, args))

    monkeypatch.setattr(daemon.os, "execvp", fake_execvp)
    daemon.main()

    assert data_dir.is_dir()
    (file, args) = exec_calls[0]
    assert file == "milvus-lite"
    assert args[args.index("--data-dir") + 1] == str(data_dir)
    assert args[args.index("--port") + 1] == "19777"
