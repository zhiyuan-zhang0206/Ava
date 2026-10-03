"""Contract: scans the tests of every tool under scripts/ for the home of their sample trees."""

from __future__ import annotations

import ast
import pathlib

import pytest

from scripts.structure import placement

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("name", "home"),
    [
        ("test_path_imports.py", "scripts/structure"),
        ("test_placement_dependencies.py", "scripts/structure"),
        ("test_coverage_gates.py", "scripts/ci"),
        ("test_lint_doc_roster.py", "scripts/content_lint"),
        ("test_lint_time_bomb.py", "scripts/lint"),
        ("test_repo_change.py", "base/deploy/git"),
    ],
)
def test_tests_of_tools_that_carry_sample_paths_and_source_have_one_home(
    name: str, home: str
) -> None:
    """Their sample trees and sample source named other units; the tool under test is their home."""
    found_files = [
        p for top in ("tests", "scripts", "base") for p in (_REPO_ROOT / top).rglob(name)
    ]
    assert len(found_files) == 1, found_files
    path = found_files[0]
    rel = path.relative_to(_REPO_ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = placement.place(rel, tree, placement.ModuleIndex(_REPO_ROOT))
    assert (found.home, found.ambiguous) == (home, False)
