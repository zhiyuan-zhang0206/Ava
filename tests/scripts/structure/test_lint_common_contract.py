"""Contract: the framework directory list of scripts/structure/lint_common.py equals the packages pyproject.toml declares."""

from __future__ import annotations

import tomllib
from pathlib import Path

from scripts.structure import lint_common

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_framework_dirs_are_the_declared_packages() -> None:
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    root_packages = pyproject["tool"]["importlinter"]["root_packages"]
    assert set(lint_common.FRAMEWORK_DIRS) == set(wheel) == set(root_packages)
