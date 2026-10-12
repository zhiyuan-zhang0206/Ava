"""Fixtures shared by the meizel tests."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, build_repo


@pytest.fixture
def make_repo(tmp_path: Path) -> RepoFactory:
    def make(files: Mapping[str, str]) -> Path:
        return build_repo(tmp_path, files)

    return make
