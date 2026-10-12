"""The file ceiling and the directory cap, evaluated against a temporary Git repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from meizel.budgets import directory_budget, limits

_STRUCTURE = ("base", "tests")


def _repo(root: Path, files: dict[str, int]) -> Path:
    for name, lines in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n" * lines, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)  # noqa: S603
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)  # noqa: S603
    return root


@pytest.mark.parametrize(("lines", "flagged"), [(800, False), (801, True)])
def test_a_file_over_the_ceiling_is_flagged(tmp_path: Path, lines: int, flagged: bool) -> None:
    repo = _repo(tmp_path, {"base/big.py": lines})
    errors = limits.check([repo / "base"], [repo], repo, _STRUCTURE)
    assert bool(errors) is flagged
    if flagged:
        assert errors == [
            f"base/big.py:{lines}: file is {lines} lines, over the 800-line hard ceiling — split it"
        ]


@pytest.mark.parametrize(("entries", "flagged"), [(20, False), (21, True)])
def test_a_directory_over_the_cap_is_flagged(tmp_path: Path, entries: int, flagged: bool) -> None:
    repo = _repo(tmp_path, {f"base/pkg/m{i}.py": 1 for i in range(entries)})
    errors = limits.check([repo / "base"], [repo], repo, _STRUCTURE)
    assert bool(errors) is flagged
    if flagged:
        assert errors == [
            f"base/pkg: directory has {entries} direct entries, over the 20-entry cap — split it"
        ]


def test_the_file_scope_skips_hidden_cache_and_migration_directories(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {"base/ok.py": 1, "base/.hidden/big.py": 900, "base/migrations/big.py": 900},
    )
    assert limits.budget_files([repo / "base"], repo, _STRUCTURE) == {repo / "base" / "ok.py"}


def test_tracked_children_rejects_a_listing_without_a_terminator() -> None:
    with pytest.raises(ValueError, match="NUL-terminated"):
        directory_budget.tracked_children("a/b.py")


def test_tracked_children_lists_every_directory_level() -> None:
    children = directory_budget.tracked_children("a/b/c.py\0a/d.py\0")
    assert children == {"": {"a"}, "a": {"b", "d.py"}, "a/b": {"c.py"}}
