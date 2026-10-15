from __future__ import annotations

import tomllib
from pathlib import Path

from meizel import lint_common
from meizel.roles import packages

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_framework_dirs_are_the_declared_packages() -> None:
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert set(lint_common.FRAMEWORK_DIRS) == set(wheel) == set(packages("application"))
