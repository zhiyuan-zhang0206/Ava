"""The permissions-helper build directory follows `AVA_HOME` after import.

`lifecycle` once bound `base.paths.permissions_helper_app_dir()` as a module
constant; `_build_directory(None)` now derives it when asked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.desktop.permissions_helper import lifecycle


def test_the_default_build_directory_follows_the_variable_after_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv("AVA_HOME", str(first))
    assert lifecycle._build_directory(None) == first / "helper"
    monkeypatch.setenv("AVA_HOME", str(second))
    assert lifecycle._build_directory(None) == second / "helper"
