"""Argument-shape and path-composition tests for the release-operator layout."""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.release_operator.layout import (
    prepare_work_dir,
    prepare_work_root,
    receipt_path,
    releases_store,
    require_commit_shape,
)

_COMMIT = "a" * 40


@pytest.mark.parametrize("bad", ["", "A" * 40, "a" * 39, "a" * 41, "not-a-sha", "a" * 39 + "g"])
def test_require_commit_shape_refuses_non_hex_or_wrong_length(bad: str) -> None:
    with pytest.raises(ValueError, match="commit"):
        require_commit_shape(bad)


def test_require_commit_shape_accepts_exact_lowercase_sha() -> None:
    require_commit_shape(_COMMIT)  # does not raise


def test_paths_compose_under_the_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert releases_store(home) == home / "releases"
    assert prepare_work_root(home) == home / "releases" / "work"
    assert prepare_work_dir(home, _COMMIT) == home / "releases" / "work" / _COMMIT
    assert receipt_path(home, _COMMIT) == home / "releases" / "work" / _COMMIT / "receipt.json"


def test_prepare_work_dir_rejects_a_malshaped_commit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="commit"):
        prepare_work_dir(tmp_path / "home", "../escape")


def test_receipt_path_rejects_a_malshaped_commit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="commit"):
        receipt_path(tmp_path / "home", "short")
