"""What counts as a direct entry for the 20-entry directory budget."""

from __future__ import annotations

import json
import pathlib

import pytest

from scripts import lint_code_structure as lcs


@pytest.fixture(autouse=True)
def _isolated_repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every main() call scans only its own temporary root, with an empty baseline."""
    monkeypatch.setattr(lcs, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("LINT_STRUCTURE_BASELINE_BASE", raising=False)
    baseline = tmp_path / "scripts/structure/baseline.json"
    baseline.parent.mkdir(parents=True)
    empty = {"directories": {}, "files": {}, "complexity": {}, "nesting": {}}
    baseline.write_text(json.dumps(empty) + "\n", encoding="utf-8")


def _module(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n", encoding="utf-8")


def test_subdirectories_ci_never_checks_out_do_not_count(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A package renamed or removed locally can leave a directory holding only
    `__pycache__` (or nothing, or only hidden files); a fresh CI checkout never
    has it, so counting it would fail a commit locally that passes in CI."""
    package = tmp_path / "tests/package"
    for index in range(20):
        _module(package / f"entry_{index}.py")
    (package / "removed_pkg" / "__pycache__").mkdir(parents=True)
    (package / "removed_pkg" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"")
    (package / "empty").mkdir()
    (package / "hidden_only").mkdir()
    (package / "hidden_only" / ".DS_Store").write_bytes(b"")
    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""

    _module(package / "real_pkg" / "module.py")
    assert lcs.main([]) == 1
    assert "tests/package: directory has 21 direct entries" in capsys.readouterr().out
