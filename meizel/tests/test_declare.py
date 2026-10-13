"""`sync --declare`: write the legal new dependencies of the changed files into the manifests."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, analysis_of, commit, failures, git, run, write

BASE = {
    "pkg/__init__.py": "",
    "pkg/a.py": "from pkg import b\n",
    "pkg/b.py": "VALUE = 1\n",
    "app/__init__.py": "",
    "app/run.py": "from pkg import a\n",
}


def declare(root: Path, *paths: str) -> int:
    """`sync --declare`, then stage what it wrote: `check` reads tracked files only."""
    code = run(root, "sync", "--declare", *paths)
    git(root, "add", "-A")
    return code


def toml_of(root: Path, directory: str) -> dict[str, object]:
    path = root / directory / "meizel.toml" if directory else root / "meizel.toml"
    return tomllib.loads(path.read_text())


def test_a_new_dependency_is_declared_where_the_two_sides_meet(make_repo: RepoFactory) -> None:
    root = make_repo(BASE)
    assert declare(root, ".") == 0
    assert toml_of(root, "pkg") == {"uses": {"a.py": ["b.py"]}, "exports": ["a.py"]}
    assert toml_of(root, "") == {"uses": {"app": ["pkg"]}}
    assert failures(root) == set()


def test_the_output_names_each_declaration(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(BASE)
    assert declare(root, ".") == 0
    out = capsys.readouterr().out
    assert "pkg/meizel.toml: declared a.py -> b.py" in out
    assert "pkg/meizel.toml: exported a.py" in out


def test_only_the_named_paths_are_declared(make_repo: RepoFactory) -> None:
    root = make_repo(BASE)
    assert declare(root, "app") == 0
    assert toml_of(root, "") == {"uses": {"app": ["pkg"]}}
    assert toml_of(root, "pkg") == {"exports": ["a.py"]}, "app/run.py needs a.py exported"
    assert ("undeclared-edge", "pkg/a.py") in failures(root)


def test_existing_entries_are_merged_and_comments_survive(make_repo: RepoFactory) -> None:
    manifest = (
        "# why pkg looks like this\n"
        'exports = ["b.py"]\n\n'
        "[uses]\n"
        '"b.py" = []\n'
        '"c.py" = ["b.py"]  # keep\n\n'
        '[debt.restricted-python]\n"c.py#exec of source text" = 1\n'
    )
    files = {**BASE, "pkg/c.py": "from pkg import b\nexec('x = 1')\n", "pkg/meizel.toml": manifest}
    root = make_repo(files)
    assert declare(root, ".") == 0
    text = (root / "pkg/meizel.toml").read_text()
    assert text.startswith("# why pkg looks like this\nexports = [")
    assert '"c.py" = ["b.py"]  # keep' in text
    assert '[debt.restricted-python]\n"c.py#exec of source text" = 1' in text
    parsed = toml_of(root, "pkg")
    assert parsed["uses"]["a.py"] == ["b.py"]  # type: ignore[index]
    assert parsed["exports"] == ["a.py", "b.py"]


def test_a_dependency_that_would_close_a_cycle_stays_an_error(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {
        **BASE,
        "pkg/b.py": "from pkg import a\n",
        "pkg/meizel.toml": '[uses]\n"a.py" = ["b.py"]\n',
    }
    root = make_repo(files)
    assert declare(root, "pkg") == 1
    out = capsys.readouterr().out
    assert "pkg/b.py:1 -> pkg/a.py" in out
    assert "closes a cycle" in out
    assert toml_of(root, "pkg")["uses"] == {"a.py": ["b.py"]}
    assert ("undeclared-edge", "pkg/b.py") in failures(root)


def test_two_new_edges_that_would_form_a_cycle_declare_one(make_repo: RepoFactory) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "from pkg import b\n",
        "pkg/b.py": "from pkg import a\n",
    }
    root = make_repo(files)
    assert declare(root, "pkg") == 1
    assert toml_of(root, "pkg")["uses"] == {"a.py": ["b.py"]}


def test_a_private_name_is_left_for_the_code_to_fix(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {**BASE, "pkg/a.py": "from pkg.b import _hidden\n", "pkg/b.py": "_hidden = 1\n"}
    root = make_repo(files)
    assert declare(root, "pkg") == 1
    assert "private name" in capsys.readouterr().out
    assert not (root / "pkg/meizel.toml").exists()


def test_an_underscore_module_is_left_for_the_code_to_fix(make_repo: RepoFactory) -> None:
    files = {
        "top/__init__.py": "",
        "top/x.py": "from top.y import _impl\n",
        "top/y/__init__.py": "",
        "top/y/_impl.py": "",
    }
    root = make_repo(files)
    assert declare(root, "top") == 1
    assert not (root / "top/meizel.toml").exists()
    assert not (root / "top/y/meizel.toml").exists()


def test_a_restricted_form_is_never_declared(make_repo: RepoFactory) -> None:
    root = make_repo({"app/__init__.py": "", "app/x.py": "exec('1')\n"})
    assert declare(root, ".") == 0
    assert not (root / "app/meizel.toml").exists()
    assert ("restricted-python", "app/x.py") in failures(root)


def test_exports_are_added_along_the_path_but_never_for_an_underscore_child(
    make_repo: RepoFactory,
) -> None:
    files = {
        "top/__init__.py": "",
        "top/x.py": "from top.y.z import leaf\n",
        "top/y/__init__.py": "",
        "top/y/z/__init__.py": "",
        "top/y/z/leaf.py": "",
    }
    root = make_repo(files)
    assert declare(root, "top") == 0
    assert toml_of(root, "top/y") == {"exports": ["z"]}
    assert toml_of(root, "top/y/z") == {"exports": ["leaf.py"]}
    assert failures(root) == set()


def test_declare_also_prunes_what_nothing_needs_any_more(make_repo: RepoFactory) -> None:
    files = {
        **BASE,
        "pkg/meizel.toml": '[uses]\n"a.py" = ["b.py"]\n"b.py" = ["__init__.py"]\n',
    }
    root = make_repo(files)
    assert declare(root, "pkg") == 0
    assert toml_of(root, "pkg")["uses"] == {"a.py": ["b.py"]}


def test_declare_never_touches_the_debt(make_repo: RepoFactory) -> None:
    files = {
        **BASE,
        "pkg/meizel.toml": '[debt.private-name]\n"x.py -> y.py#_n" = 3\n',
    }
    root = make_repo(files)
    assert declare(root, "pkg") == 0
    assert toml_of(root, "pkg")["debt"] == {"private-name": {"x.py -> y.py#_n": 3}}


def test_declare_twice_changes_nothing(make_repo: RepoFactory) -> None:
    root = make_repo(BASE)
    assert declare(root, ".") == 0
    before = {p: p.read_text() for p in root.rglob("meizel.toml")}
    assert declare(root, ".") == 0
    assert {p: p.read_text() for p in root.rglob("meizel.toml")} == before


def test_without_paths_the_changed_files_are_declared(make_repo: RepoFactory) -> None:
    root = make_repo({**BASE, "pkg/a.py": "VALUE = 2\n", "pkg/c.py": "from pkg import b\n"})
    commit(root)
    write(root, {"app/run.py": "# changed\nfrom pkg import a\n"})
    assert declare(root) == 0
    assert toml_of(root, "") == {"uses": {"app": ["pkg"]}}
    assert toml_of(root, "pkg") == {"exports": ["a.py"]}, "pkg/c.py did not change"


def test_without_paths_the_branch_is_compared_with_origin_main(make_repo: RepoFactory) -> None:
    root = make_repo({**BASE, "pkg/a.py": "VALUE = 2\n", "pkg/c.py": "from pkg import b\n"})
    commit(root)
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    write(root, {"app/run.py": "# changed\nfrom pkg import a\n"})
    commit(root, "branch work")
    assert declare(root) == 0
    assert toml_of(root, "") == {"uses": {"app": ["pkg"]}}
    assert toml_of(root, "pkg") == {"exports": ["a.py"]}


def test_sync_needs_an_action(make_repo: RepoFactory) -> None:
    assert run(make_repo(BASE), "sync") == 2


def test_a_loads_declaration_edge_is_declared(make_repo: RepoFactory) -> None:
    files = {
        "lib/__init__.py": "",
        "app/__init__.py": "",
        "app/loader.py": "import importlib\nimportlib.import_module('lib')\n",
        "app/meizel.toml": '[loads]\n"loader.py" = ["lib"]\n',
    }
    root = make_repo(files)
    assert declare(root, "app") == 0
    assert toml_of(root, "") == {"uses": {"app": ["lib"]}}
    assert analysis_of(root).verdict == []
