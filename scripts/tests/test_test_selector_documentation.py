"""Documentation routing follows shared runtime resource facts in both trees."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.ci import test_selector

_DOC = "docs/runtime.md"
_TEST = "base/consumer/tests/test_document.py"
_READER = """from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
def test_document():
    assert (ROOT / "docs/runtime.md").read_text()
"""


def _write(root: Path, path: str, source: str) -> None:
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(source)


def _repo(root: Path, source: str = _READER) -> Path:
    _write(root, "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["base/**/tests"]\n')
    _write(root, _TEST, source)
    _write(root, _DOC, "runtime input\n")
    _write(root, "base/unrelated/tests/test_filler.py", "def test_filler(): pass\n")
    _write(
        root,
        ".test_durations",
        json.dumps({"base/unrelated/tests/test_filler.py::test_filler": 1000}),
    )
    return root


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed Git commands against a test-owned checkout
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_documentation_with_a_runtime_reader_selects_backend_tests(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert test_selector.documentation_runtime_tests([_DOC], repo_root=root) == {_TEST}
    result = test_selector.select_tests([_DOC], repo_root=root)
    assert result.decision == "SELECTED", result.as_json()
    assert result.tests == (_TEST,)


def test_known_document_reader_keeps_unknown_diagnostics_visible(tmp_path: Path) -> None:
    root = _repo(tmp_path, _READER + "import importlib\nimportlib.import_module(module_name)\n")
    assert test_selector.documentation_runtime_tests([_DOC], repo_root=root) == {_TEST}
    result = test_selector.select_tests([_DOC], repo_root=root)
    assert (result.decision, result.reason) == ("FULL", "incomplete-impact")
    assert any(_TEST in diagnostic for diagnostic in result.diagnostics)


def test_unread_documentation_still_skips_despite_unrelated_unknown(tmp_path: Path) -> None:
    root = _repo(tmp_path, "import importlib\nimportlib.import_module(module_name)\n")
    assert not test_selector.documentation_runtime_tests([_DOC], repo_root=root)
    result = test_selector.select_tests([_DOC], repo_root=root)
    assert (result.decision, result.reason) == ("SKIP", "docs-only")


@pytest.mark.parametrize("delete_doc", [False, True])
def test_base_readers_keep_removed_document_inputs_in_backend_routing(
    tmp_path: Path, delete_doc: bool
) -> None:
    root = _repo(tmp_path)
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    _write(root, _TEST, "def test_document(): pass\n")
    if delete_doc:
        (root / _DOC).unlink()
    assert not test_selector.documentation_runtime_tests([_DOC], repo_root=root)
    assert test_selector.documentation_runtime_tests([_DOC], repo_root=root, base_ref="HEAD") == {
        _TEST
    }
    result = test_selector.select_tests([_DOC], repo_root=root, base_ref="HEAD")
    assert result.decision == "SELECTED", result.as_json()
    assert result.tests == (_TEST,)


def test_document_resource_analysis_does_not_hide_parser_errors(tmp_path: Path) -> None:
    root = _repo(tmp_path, "def syntax error\n")
    with pytest.raises(SyntaxError):
        test_selector.documentation_runtime_tests([_DOC], repo_root=root)
