"""Fact edges: which file uses which, derived from import statements and attributes."""

from __future__ import annotations

from pathlib import Path

from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, edges


def test_from_import_targets_the_submodule_when_one_exists_else_the_door(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "def helper(): ...\n",
            "pkg/a.py": "from pkg import b\nfrom pkg import helper\n",
            "pkg/b.py": "",
        }
    )
    found = edges(root)
    assert ("pkg/a.py", "pkg/b.py", "import") in found
    assert ("pkg/a.py", "pkg/__init__.py", "import") in found
    assert ("pkg/a.py", "pkg/helper.py", "import") not in found


def test_plain_import_targets_the_named_module_not_its_package_door(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "",
            "pkg/sub/m.py": "",
            "x.py": "import pkg.sub.m\n",
        }
    )
    assert edges(root) == {("x.py", "pkg/sub/m.py", "import")}


def test_relative_imports_resolve_against_the_importing_package(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/b.py": "def f(): ...\n",
            "pkg/sub/__init__.py": "",
            "pkg/sub/a.py": "from . import c\nfrom .. import b\nfrom ..b import f\n",
            "pkg/sub/c.py": "",
        }
    )
    found = {e for e in edges(root) if e[0] == "pkg/sub/a.py"}
    assert found == {
        ("pkg/sub/a.py", "pkg/sub/c.py", "import"),
        ("pkg/sub/a.py", "pkg/b.py", "import"),
    }


def test_relative_import_leaving_the_repository_is_reported(make_repo: RepoFactory) -> None:
    root = make_repo({"pkg/a.py": "from ... import b\n"})
    [problem] = diagnostics(root, "unresolved-import")
    assert (problem.path, problem.line) == ("pkg/a.py", 1)


def test_unresolved_first_party_import_is_reported_and_third_party_is_ignored(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "a.py": "import json\nimport numpy\nimport pkg.missing\nfrom pkg import nothing\n",
        }
    )
    [problem] = diagnostics(root, "unresolved-import")
    assert "pkg.missing" in problem.message


def test_from_import_of_a_missing_name_is_attributed_to_the_package_door(
    make_repo: RepoFactory,
) -> None:
    root = make_repo({"pkg/__init__.py": "value = 1\n", "a.py": "from pkg import value\n"})
    assert edges(root) == {("a.py", "pkg/__init__.py", "import")}


def test_imports_inside_functions_count(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/m.py": "",
            "a.py": "def run():\n    from pkg import m\n    return m\n",
        }
    )
    assert ("a.py", "pkg/m.py", "import") in edges(root)


def test_namespace_directories_resolve_like_pythonpath_dot(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "tests/fixtures/helpers.py": "VALUE = 1\n",
            "base/x/tests/test_a.py": "from tests.fixtures import helpers\nimport tests.fixtures.helpers as h\n",
        }
    )
    assert edges(root) == {("base/x/tests/test_a.py", "tests/fixtures/helpers.py", "import")}


def test_attribute_use_on_an_imported_module_records_the_used_name(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/b.py": "def f(): ...\n",
            "a.py": "import pkg.b\nfrom pkg import b as other\npkg.b.f()\nother.g()\n",
        }
    )
    uses = {
        (f.dst, f.names) for f in analysis_of(root).facts if f.kind.value == "attribute" and f.names
    }
    assert uses == {("pkg/b.py", ("f",)), ("pkg/b.py", ("g",))}


def test_attribute_chain_reaching_a_deeper_submodule_is_a_fact(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "",
            "pkg/sub/m.py": "",
            "a.py": "import pkg\npkg.sub.m\n",
        }
    )
    assert ("a.py", "pkg/sub/m.py", "attribute") in edges(root)


def test_a_shadowed_import_name_is_not_a_module_reference(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/b.py": "",
            "a.py": "from pkg import b\n\ndef run(b):\n    return b.thing\n",
        }
    )
    assert {e[2] for e in edges(root)} == {"import"}


def test_function_scope_sees_module_imports_but_not_class_scope_names(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/b.py": "def f(): ...\n",
            "a.py": "import pkg.b\n\nclass K:\n    pkg = 1\n\n    def run(self):\n        return pkg.b.f()\n",
        }
    )
    assert ("a.py", "pkg/b.py", "attribute") in edges(root)


def test_files_outside_python_are_not_scanned_and_untracked_files_are_not_nodes(
    make_repo: RepoFactory, tmp_path: Path
) -> None:
    root = make_repo({"pkg/__init__.py": "", "notes.md": "import nothing\n"})
    (root / "pkg" / "loose.py").write_text("import pkg.missing\n")
    assert edges(root) == set()
    assert diagnostics(root, "unresolved-import") == []
