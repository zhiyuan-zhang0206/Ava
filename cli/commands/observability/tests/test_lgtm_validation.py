"""The actual pinned Loki validator refuses invalid preparation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cli.commands.observability import lgtm_native
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

_REAL_VERIFY_LOKI = lgtm_native.verify_loki


def test_pinned_loki_parser_rejection_blocks_preparation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def reject(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "unknown config field")

    monkeypatch.setattr(lgtm_native.subprocess, "run", reject)
    with pytest.raises(RuntimeError, match="unknown config field"):
        _REAL_VERIFY_LOKI(tmp_path)
    assert calls == [
        [
            str(tmp_path / "lgtm/native/bin/loki"),
            f"-config.file={tmp_path}/lgtm/native/config/loki.yaml",
            "-verify-config",
        ]
    ]
