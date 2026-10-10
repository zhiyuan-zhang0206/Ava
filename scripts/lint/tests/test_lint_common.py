"""`scripts/structure/lint_common.py` — the one framework-dir list lints share.

A lint that names a directory that does not exist scans nothing there and still
passes, which is how three lints drifted onto a missing `plugins/` while the
real plugin code under `ava_builtins/` went unchecked. The list is pinned to the
packages pyproject declares, and resolving a missing directory is an error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lint import clock_lattice, termination_source
from scripts.lint.diagnostics import logger_add_diagnose, loguru_format, no_emoji, time_bomb
from scripts.structure import lint_common

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_every_framework_dir_exists() -> None:
    roots = lint_common.scan_roots(_REPO_ROOT, lint_common.FRAMEWORK_DIRS)
    assert [r.name for r in roots] == list(lint_common.FRAMEWORK_DIRS)


def test_missing_scan_dir_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "agent").mkdir()
    with pytest.raises(FileNotFoundError, match="plugins, mcps"):
        lint_common.scan_roots(tmp_path, ("agent", "plugins", "mcps"))


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


@pytest.mark.parametrize(
    "pattern", ["/**/tests", "*/**/tests", "base/**/test", "base/../tests", "base\\child/**/tests"]
)
def test_pytest_test_scope_rejects_unsupported_directory_patterns(pattern: str) -> None:
    with pytest.raises(ValueError, match="unsupported pytest testpaths"):
        lint_common.pytest_test_hosts(f"[tool.pytest.ini_options]\ntestpaths = [{pattern!r}]\n")


def test_pytest_test_scope_rejects_duplicate_hosts() -> None:
    with pytest.raises(ValueError, match="duplicate pytest test host"):
        lint_common.pytest_test_hosts('[tool.pytest.ini_options]\ntestpaths = ["tests", "tests"]\n')


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
