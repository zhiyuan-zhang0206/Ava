from pathlib import Path

import pytest

from scripts.content_lint.code_comments.cli import selected_paths
from scripts.content_lint.code_comments.tests.cases import repository


def test_explicit_check_selects_existing_prose(tmp_path: Path) -> None:
    repo = repository(tmp_path, {"legacy.py": "# Old notes\n"})
    assert selected_paths(repo, []) == frozenset({"legacy.py"})


def test_unsupported_explicit_source_does_not_claim_success(tmp_path: Path) -> None:
    repo = repository(tmp_path, {"core.rs": "// Rust\n"})
    with pytest.raises(ValueError, match="no source parser"):
        selected_paths(repo, ["core.rs"])
