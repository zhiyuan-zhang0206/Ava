"""A `path_scopes.toml` mounts a pytest plugin by name; each mounted module is one fact edge."""

from __future__ import annotations

from pathlib import Path

import pytest

from meizel.graph.model import MeizelError
from meizel.tests.synthetic import RepoFactory, diagnostics, edges, run, verdict

PLUGINS = {
    "plugins/__init__.py": "",
    "plugins/fx.py": "",
    "plugins/other.py": "",
    "plugins/meizel.toml": 'exports = ["fx.py", "other.py"]\n',
    "pkg/__init__.py": "",
    "pkg/tests/test_a.py": "def test_a():\n    pass\n",
    "pkg/tests/test_b.py": "def test_b():\n    pass\n",
}
DECLARED = {"meizel.toml": '[uses]\n"pkg" = ["plugins"]\n'}


def mount_edges(root: Path) -> set[tuple[str, str]]:
    return {(s, d) for s, d, kind in edges(root) if kind == "plugin"}


def test_a_mount_is_an_edge_from_the_toml_to_the_modules_file(make_repo: RepoFactory) -> None:
    root = make_repo({**PLUGINS, "pkg/tests/path_scopes.toml": '"plugins.fx" = ["."]\n'})
    assert mount_edges(root) == {("pkg/tests/path_scopes.toml", "plugins/fx.py")}


def test_one_edge_per_mounted_module_however_many_paths_it_governs(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            **PLUGINS,
            "pkg/tests/path_scopes.toml": (
                '"plugins.fx" = ["test_a.py", "test_b.py"]\n"plugins.other" = ["."]\n'
            ),
        }
    )
    assert mount_edges(root) == {
        ("pkg/tests/path_scopes.toml", "plugins/fx.py"),
        ("pkg/tests/path_scopes.toml", "plugins/other.py"),
    }


def test_a_mount_with_no_paths_governs_nothing_and_is_no_edge(make_repo: RepoFactory) -> None:
    root = make_repo({**PLUGINS, "pkg/tests/path_scopes.toml": '"plugins.fx" = []\n'})
    assert mount_edges(root) == set()


def test_an_undeclared_mount_is_judged_like_an_import(make_repo: RepoFactory) -> None:
    root = make_repo(
        {**PLUGINS, "pkg/tests/path_scopes.toml": '"plugins.other" = ["."]\n"plugins.fx" = ["."]\n'}
    )
    found = diagnostics(root, "undeclared-edge")
    assert {(d.path, d.line) for d in found} == {
        ("pkg/tests/path_scopes.toml", 1),
        ("pkg/tests/path_scopes.toml", 2),
    }
    assert all("path_scopes mount" in d.message for d in found)


def test_a_declared_mount_is_clean(make_repo: RepoFactory) -> None:
    root = make_repo(
        {**PLUGINS, **DECLARED, "pkg/tests/path_scopes.toml": '"plugins.fx" = ["."]\n'}
    )
    assert verdict(root) == set()


def test_a_mount_through_a_file_the_directory_does_not_export_is_refused(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            **PLUGINS,
            **DECLARED,
            "plugins/meizel.toml": 'exports = ["other.py"]\n',
            "pkg/tests/path_scopes.toml": '"plugins.fx" = ["."]\n',
        }
    )
    assert {d.path for d in diagnostics(root, "not-exported")} == {"pkg/tests/path_scopes.toml"}


def test_a_mount_of_a_module_that_does_not_exist_fails(make_repo: RepoFactory) -> None:
    root = make_repo(
        {**PLUGINS, **DECLARED, "pkg/tests/path_scopes.toml": '# mounts\n"plugins.gone" = ["."]\n'}
    )
    (found,) = diagnostics(root, "unresolved-import")
    assert (found.path, found.line) == ("pkg/tests/path_scopes.toml", 2)
    assert "plugins.gone" in found.message
    assert mount_edges(root) == set()


def test_a_mount_is_judged_when_its_toml_changes_and_not_otherwise(make_repo: RepoFactory) -> None:
    root = make_repo({**PLUGINS, "pkg/tests/path_scopes.toml": '"plugins.fx" = ["."]\n'})
    assert run(root, "check", "--changed", "pkg/tests/path_scopes.toml") == 1
    assert run(root, "check", "--changed", "pkg/tests/test_a.py") == 0


def test_a_toml_that_does_not_parse_stops_the_analysis(make_repo: RepoFactory) -> None:
    root = make_repo({**PLUGINS, "pkg/tests/path_scopes.toml": '"plugins.fx" = [\n'})
    with pytest.raises(MeizelError, match=r"path_scopes\.toml: invalid TOML"):
        verdict(root)
