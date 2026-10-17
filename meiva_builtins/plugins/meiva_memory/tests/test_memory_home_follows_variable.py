from __future__ import annotations

from pathlib import Path

import pytest

from meiva_builtins.plugins.meiva_memory import pool_ops, sdk


def test_pool_dir_follows_the_variable_after_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv("MEIVA_HOME", str(first))
    assert pool_ops.pool_dir() == first / "memory"
    monkeypatch.setenv("MEIVA_HOME", str(second))
    assert pool_ops.pool_dir() == second / "memory"


def test_sdk_path_follows_the_variable_after_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv("MEIVA_HOME", str(first))
    assert first / "memory" == sdk.PATH
    monkeypatch.setenv("MEIVA_HOME", str(second))
    assert second / "memory" == sdk.PATH
