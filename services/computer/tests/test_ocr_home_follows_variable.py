"""The OCR helper binary path follows `AVA_HOME` after the module is imported.

`services.computer.ocr` once bound `logs_dir() / "computer" / "ocr-bin"` when it was
imported; `_bin_path()` now derives it when asked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.computer import ocr


def test_the_binary_path_follows_the_variable_after_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv("AVA_HOME", str(first))
    assert ocr._bin_path() == first / "logs" / "computer" / "ocr-bin" / "ocr"
    monkeypatch.setenv("AVA_HOME", str(second))
    assert ocr._bin_path() == second / "logs" / "computer" / "ocr-bin" / "ocr"
