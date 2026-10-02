"""`scripts/structure/lint_common.py` — the one framework-dir list lints share.

A lint that names a directory that does not exist scans nothing there and still
passes, which is how three lints drifted onto a missing `plugins/` while the
real plugin code under `ava_builtins/` went unchecked. The list is pinned to the
packages pyproject declares, and resolving a missing directory is an error.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from scripts.lint import (
    clock_lattice,
    code_structure,
    logger_add_diagnose,
    loguru_format,
    no_emoji,
    termination_source,
    time_bomb,
)
from scripts.structure import lint_common

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_framework_dirs_are_the_declared_packages() -> None:
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    root_packages = pyproject["tool"]["importlinter"]["root_packages"]
    assert set(lint_common.FRAMEWORK_DIRS) == set(wheel) == set(root_packages)


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
        code_structure._SCAN_DIRS,
        no_emoji._SCAN_DIRS,
        termination_source._SCAN_DIRS,
        time_bomb._SCAN_DIRS,
        logger_add_diagnose._SCAN_DIRS,
        loguru_format._SCAN_DIRS,
    ],
)
def test_framework_scoped_lints_cover_every_framework_dir(scan_dirs: tuple[str, ...]) -> None:
    assert set(lint_common.FRAMEWORK_DIRS) <= set(scan_dirs)
    lint_common.scan_roots(_REPO_ROOT, scan_dirs)
