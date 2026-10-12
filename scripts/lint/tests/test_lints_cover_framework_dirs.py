"""Every framework-scoped lint covers every framework directory of `meizel.lint_common`."""

from __future__ import annotations

from pathlib import Path

import pytest

from meizel import lint_common
from meizel.owners import clock_lattice
from scripts.lint import termination_source
from scripts.lint.diagnostics import logger_add_diagnose, loguru_format, no_emoji, time_bomb

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "scan_dirs",
    [
        clock_lattice._SCAN_DIRS,
        no_emoji._SCAN_DIRS,
        termination_source._SCAN_DIRS,
        logger_add_diagnose._SCAN_DIRS,
        loguru_format._SCAN_DIRS,
    ],
)
def test_framework_scoped_lints_cover_every_framework_dir(scan_dirs: tuple[str, ...]) -> None:
    assert set(lint_common.FRAMEWORK_DIRS) <= set(scan_dirs)
    lint_common.scan_roots(_REPO_ROOT, scan_dirs)


@pytest.mark.parametrize("directory", (*lint_common.FRAMEWORK_DIRS, "scripts"))
def test_time_bomb_default_scan_covers_every_framework_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], directory: str
) -> None:
    for name in (*lint_common.FRAMEWORK_DIRS, "scripts", "tests"):
        (tmp_path / name).mkdir()
    target = tmp_path / directory / "tests" / "test_window.py"
    target.parent.mkdir()
    target.write_text('since = "2026-09-06"\n', encoding="utf-8")
    assert time_bomb.main([], repo_root=tmp_path) == 1
    assert f"{target}:1: time-bomb fixture date" in capsys.readouterr().err
