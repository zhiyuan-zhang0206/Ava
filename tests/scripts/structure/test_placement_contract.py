"""Contract: scans the tests of every tool under scripts/ for the home of their sample trees."""

from __future__ import annotations

import ast
import pathlib

import pytest

from scripts.structure import imports, placement
from scripts.structure.tests.patch_repo import make_repo, write

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("name", "home"),
    [
        ("test_path_imports.py", "scripts/structure"),
        ("test_placement.py", "scripts/structure"),
        ("test_placement_dependencies.py", "scripts/structure"),
        ("test_coverage_gates.py", "scripts/ci"),
        ("test_lint_doc_roster.py", "scripts/content_lint"),
        ("test_lint_time_bomb.py", "scripts/lint/diagnostics"),
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


@pytest.mark.parametrize("package_tests", [False, True])
def test_pytest_importlib_relative_imports_match_ast_with_or_without_test_initializer(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, package_tests: bool
) -> None:
    """Exercise Python's package anchor, including pytest's namespace test directories."""
    root = make_repo(pytester.path)
    initializer = "from .retry import backoff as local_backoff\n"
    write(root, "base/net/__init__.py", initializer)
    if package_tests:
        write(root, "base/net/tests/__init__.py", "")
    rel = "base/net/tests/test_relative.py"
    text = (
        "from .. import retry as subject\n"
        "from ..retry import backoff as call\n"
        "from .. import local_backoff as exported\n\n"
        "import base.net.retry as absolute_subject\n"
        "from ..retry import backoff as first, backoff as second\n\n"
        "def test_relative_contract():\n"
        "    from ..retry import backoff as lazy\n"
        "    assert __package__ == 'base.net.tests'\n"
        "    assert call() == subject.backoff() == exported() == absolute_subject.backoff() == 1\n"
        "    assert first() == second() == lazy() == 1\n"
    )
    write(root, rel, text)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    result = pytester.runpytest_subprocess(
        "-q",
        "--import-mode=importlib",
        "--confcutdir",
        str(root),
        "-o",
        "addopts=",
        rel,
        timeout=30,
    )
    result.assert_outcomes(passed=1)
    index = placement.ModuleIndex(root)
    refs = placement.collect_references(ast.parse(text), index, rel)
    assert [ref.module for ref in refs] == [
        "base.net.retry",
        "base.net.retry",
        "base.net",
        "base.net.retry",
        "base.net.retry",
        "base.net.retry",
    ]
    assert refs[4].names == ("first", "second")
    init_refs = placement.collect_references(ast.parse(initializer), index, "base/net/__init__.py")
    assert [(ref.module, ref.names) for ref in init_refs] == [
        ("base.net.retry", ("local_backoff",))
    ]


@pytest.mark.parametrize(
    ("rel", "text", "module", "error"),
    [
        ("base/__init__.py", "from ..base.net import retry", "base", "beyond top-level package"),
        ("standalone.py", "from .base.net import retry", "standalone", "no known parent package"),
    ],
)
def test_relative_imports_cannot_escape_the_package_into_repository_modules(
    pytester: pytest.Pytester, rel: str, text: str, module: str, error: str
) -> None:
    root = make_repo(pytester.path)
    write(root, rel, text)
    probe = pytester.makepyfile(probe=f"import importlib\nimportlib.import_module({module!r})\n")
    result = pytester.runpython(probe)
    assert result.ret != 0
    assert error in "\n".join(result.errlines)
    with pytest.raises(imports.InvalidRelativeImportError, match=rel):
        placement.collect_references(ast.parse(text), placement.ModuleIndex(root), rel)
