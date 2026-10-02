"""The memory pool path follows `AVA_HOME` after the modules are imported.

`pool_ops.pool_dir()` and `ava.memory.PATH` (a module-level `__getattr__` on the
plugin's `sdk`) once bound the home when they were imported. Each now derives it
when asked, so a process that changes the variable is followed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ava_builtins.plugins.ava_memory import pool_ops, sdk


def test_pool_dir_follows_the_variable_after_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv("AVA_HOME", str(first))
    assert pool_ops.pool_dir() == first / "memory"
    monkeypatch.setenv("AVA_HOME", str(second))
    assert pool_ops.pool_dir() == second / "memory"


def test_sdk_path_follows_the_variable_after_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv("AVA_HOME", str(first))
    assert first / "memory" == sdk.PATH  # pyright: ignore[reportAttributeAccessIssue]  # module __getattr__
    monkeypatch.setenv("AVA_HOME", str(second))
    assert second / "memory" == sdk.PATH  # pyright: ignore[reportAttributeAccessIssue]  # module __getattr__
