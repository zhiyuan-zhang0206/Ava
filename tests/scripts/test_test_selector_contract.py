"""Contract: every lint-family test file on disk is pinned in the selector, and no pin is stale."""

from __future__ import annotations

from pathlib import Path

from scripts.ci import test_selector

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_tree_scan_pins_cover_the_real_repo_lint_family_and_are_never_stale() -> None:
    """Guard: every lint-family file on disk is pinned, and every explicit pin
    exists — a new scan test cannot silently miss the subset."""
    pinned = test_selector.tree_scan_tests(_REPO_ROOT)
    lint_files = {
        path.relative_to(_REPO_ROOT).as_posix()
        for path in (_REPO_ROOT / "tests").rglob("test_lint_*.py")
        if not path.relative_to(_REPO_ROOT).as_posix().startswith("tests/e2e/")
    }
    assert lint_files, "the lint family must be discoverable"
    assert lint_files <= pinned
    for path in test_selector._TREE_SCAN_TESTS:
        assert path in pinned, f"{path} is stale (missing on disk)"
