"""The rule: declared edges, exports along the path, private names, acyclic local graphs."""

from __future__ import annotations

from scripts.meizel.tests.synthetic import RepoFactory, diagnostics, verdict

TWO_FILES = {
    "pkg/__init__.py": "",
    "pkg/a.py": "from pkg import b\n",
    "pkg/b.py": "VALUE = 1\n",
}


def test_declared_sibling_edge_passes(make_repo: RepoFactory) -> None:
    root = make_repo({**TWO_FILES, "pkg/meizel.toml": '[uses]\n"a.py" = ["b.py"]\n'})
    assert verdict(root) == set()


def test_undeclared_edge_names_the_fact_the_directory_and_the_line_to_add(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(TWO_FILES)
    [found] = diagnostics(root, "undeclared-edge")
    assert (found.path, found.line) == ("pkg/a.py", 1)
    text = found.render()
    assert "pkg/a.py -> pkg/b.py" in text
    assert "L = pkg" in text
    assert 'pkg/meizel.toml:  [uses]  "a.py" = [..., "b.py"]' in text


def test_edge_is_judged_in_the_lowest_common_directory(make_repo: RepoFactory) -> None:
    files = {
        "top/__init__.py": "",
        "top/x/__init__.py": "",
        "top/x/m.py": "from top.y import n\n",
        "top/y/__init__.py": "",
        "top/y/n.py": "",
        "top/meizel.toml": '[uses]\n"x" = ["y"]\n',
        "top/y/meizel.toml": 'exports = ["n.py"]\n',
    }
    assert verdict(make_repo(files)) == set()


def test_the_declared_direction_matters(make_repo: RepoFactory) -> None:
    files = {
        "top/x/m.py": "from top.y import n\n",
        "top/y/n.py": "",
        "top/meizel.toml": '[uses]\n"y" = ["x"]\n',
        "top/y/meizel.toml": 'exports = ["n.py"]\n',
    }
    found = verdict(make_repo(files))
    assert ("undeclared-edge", "top/x/m.py") in found
    assert ("manifest-stale-edge", "top/meizel.toml") in found


PATH_FILES = {
    "top/x.py": "from top.y.z import leaf\n",
    "top/y/__init__.py": "",
    "top/y/z/__init__.py": "",
    "top/y/z/leaf.py": "",
    "top/meizel.toml": '[uses]\n"x.py" = ["y"]\n',
}


def test_the_first_directory_on_the_path_must_export_the_next_child(make_repo: RepoFactory) -> None:
    [found] = diagnostics(make_repo(PATH_FILES), "not-exported")
    assert "enters top/y through 'z'" in found.message
    assert 'top/y/meizel.toml:  exports = [..., "z"]' in found.render()


def test_every_later_directory_on_the_path_must_export_too(make_repo: RepoFactory) -> None:
    files = {**PATH_FILES, "top/y/meizel.toml": 'exports = ["z"]\n'}
    [found] = diagnostics(make_repo(files), "not-exported")
    assert "enters top/y/z through 'leaf.py'" in found.message


def test_a_fully_exported_path_passes(make_repo: RepoFactory) -> None:
    files = {
        "top/x.py": "from top.y.z import leaf\n",
        "top/y/__init__.py": "",
        "top/y/z/__init__.py": "",
        "top/y/z/leaf.py": "",
        "top/meizel.toml": '[uses]\n"x.py" = ["y"]\n',
        "top/y/meizel.toml": 'exports = ["z"]\n',
        "top/y/z/meizel.toml": 'exports = ["leaf.py"]\n',
    }
    assert verdict(make_repo(files)) == set()


def test_the_package_door_needs_no_export(make_repo: RepoFactory) -> None:
    files = {
        "top/x.py": "from top.y import thing\n",
        "top/y/__init__.py": "thing = 1\n",
        "top/meizel.toml": '[uses]\n"x.py" = ["y"]\n',
    }
    assert verdict(make_repo(files)) == set()


def test_a_nested_package_door_needs_only_the_path_to_it_exported(make_repo: RepoFactory) -> None:
    files = {
        "top/x.py": "from top.y.z import thing\n",
        "top/y/__init__.py": "",
        "top/y/z/__init__.py": "thing = 1\n",
        "top/meizel.toml": '[uses]\n"x.py" = ["y"]\n',
        "top/y/meizel.toml": 'exports = ["z"]\n',
    }
    assert verdict(make_repo(files)) == set()


def test_an_underscore_child_is_visible_to_siblings_but_never_across_the_boundary(
    make_repo: RepoFactory,
) -> None:
    sibling = {
        "pkg/__init__.py": "",
        "pkg/a.py": "from pkg import _impl\n",
        "pkg/_impl.py": "",
        "pkg/meizel.toml": '[uses]\n"a.py" = ["_impl.py"]\n',
    }
    assert verdict(make_repo(sibling)) == set()


def test_crossing_the_boundary_through_an_underscore_child_is_rejected(
    make_repo: RepoFactory,
) -> None:
    files = {
        "top/x.py": "from top.y import _impl\n",
        "top/y/__init__.py": "",
        "top/y/_impl.py": "",
        "top/meizel.toml": '[uses]\n"x.py" = ["y"]\n',
    }
    [found] = diagnostics(make_repo(files), "private-module")
    assert "'_impl.py'" in found.message
    assert "top/y/__init__.py" in found.render()


def test_underscore_names_are_private_to_the_defining_file_for_production_and_tests(
    make_repo: RepoFactory,
) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "from pkg.b import _secret, public, __version__\n",
        "pkg/b.py": "_secret = 1\npublic = 2\n__version__ = '1'\n",
        "pkg/tests/test_b.py": "import pkg.b\n\ndef test_it():\n    assert pkg.b._secret\n",
        "pkg/meizel.toml": '[uses]\n"a.py" = ["b.py"]\n"tests" = ["b.py"]\n',
    }
    found = diagnostics(make_repo(files), "private-name")
    assert {(d.path, d.line) for d in found} == {("pkg/a.py", 1), ("pkg/tests/test_b.py", 4)}
    assert all("'_secret'" in d.message for d in found)


def test_a_file_may_use_its_own_private_names(make_repo: RepoFactory) -> None:
    files = {"pkg/__init__.py": "", "pkg/a.py": "import pkg.a\n_x = 1\n"}
    assert verdict(make_repo(files)) == set()


def test_declaring_an_edge_that_closes_a_cycle_is_called_out(make_repo: RepoFactory) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "from pkg import b\n",
        "pkg/b.py": "from pkg import c\n",
        "pkg/c.py": "from pkg import a\n",
        "pkg/meizel.toml": '[uses]\n"a.py" = ["b.py"]\n"b.py" = ["c.py"]\n',
    }
    [found] = diagnostics(make_repo(files), "undeclared-edge")
    assert found.path == "pkg/c.py"
    assert "closes a cycle" in found.render()


def test_a_cyclic_local_graph_fails_even_when_every_edge_is_a_real_fact(
    make_repo: RepoFactory,
) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "from pkg import b\n",
        "pkg/b.py": "from pkg import a\n",
        "pkg/meizel.toml": '[uses]\n"a.py" = ["b.py"]\n"b.py" = ["a.py"]\n',
    }
    [found] = diagnostics(make_repo(files), "manifest-cycle")
    assert "a.py -> b.py -> a.py" in found.message or "b.py -> a.py -> b.py" in found.message


def test_reexporting_a_child_that_uses_the_door_is_a_cycle(make_repo: RepoFactory) -> None:
    files = {
        "pkg/__init__.py": "from pkg.a import thing\n",
        "pkg/a.py": "from pkg import other\nthing = 1\n",
        "pkg/other.py": "",
        "pkg/meizel.toml": '[uses]\n"__init__.py" = ["a.py"]\n"a.py" = ["__init__.py", "other.py"]\n',
    }
    assert ("manifest-cycle", "pkg/meizel.toml") in verdict(make_repo(files))


def test_a_directory_without_a_manifest_exports_only_its_door(make_repo: RepoFactory) -> None:
    files = {
        "top/x.py": "from top.y import m\n",
        "top/y/__init__.py": "",
        "top/y/m.py": "",
        "top/meizel.toml": '[uses]\n"x.py" = ["y"]\n',
    }
    assert verdict(make_repo(files)) == {("not-exported", "top/x.py")}


def test_the_repository_root_has_a_local_graph_too(make_repo: RepoFactory) -> None:
    files = {
        "alpha/__init__.py": "",
        "alpha/a.py": "from beta import b\n",
        "beta/__init__.py": "",
        "beta/b.py": "",
        "meizel.toml": '[uses]\n"alpha" = ["beta"]\n',
        "beta/meizel.toml": 'exports = ["b.py"]\n',
    }
    assert verdict(make_repo(files)) == set()
