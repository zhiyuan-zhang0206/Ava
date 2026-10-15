from pathlib import Path

import pytest

from scripts.content_lint.code_comments.cli import check_paths, selected_paths
from scripts.content_lint.code_comments.tests.cases import repository


def test_changed_paths_check_whole_files_and_reject_missing_sources(tmp_path: Path) -> None:
    repo = repository(
        tmp_path, {"core.py": "# Existing comment\nvalue = 1\n", "guide.md": "A guide\n"}
    )
    changed = tmp_path / "changed"
    changed.write_text("core.py\0guide.md\0", encoding="utf-8")
    assert check_paths(repo, "check", [], changed) == frozenset({"core.py"})
    changed.write_text("missing.py\0", encoding="utf-8")
    with pytest.raises(ValueError, match="tracked"):
        check_paths(repo, "check", [], changed)


def test_empty_changed_set_does_not_request_full_migration(tmp_path: Path) -> None:
    repo = repository(tmp_path, {"legacy.py": "# Old notes\n"})
    changed = tmp_path / "changed"
    changed.write_bytes(b"")
    assert check_paths(repo, "check", [], changed) == frozenset()
    assert selected_paths(repo, []) == frozenset({"legacy.py"})


def test_unsupported_explicit_source_does_not_claim_success(tmp_path: Path) -> None:
    repo = repository(tmp_path, {"core.rs": "// Rust\n"})
    with pytest.raises(ValueError, match="no source parser"):
        selected_paths(repo, ["core.rs"])
