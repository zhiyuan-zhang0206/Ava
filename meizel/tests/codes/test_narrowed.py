"""Declared edges whose endpoints name a deeper path than a child of the directory."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, failures, git, run

LIB = {
    "lib/__init__.py": "",
    "lib/a.py": "VALUE = 1\n",
    "lib/b.py": "VALUE = 2\n",
    "lib/meizel.toml": 'exports = ["a.py", "b.py"]\n',
    "app/__init__.py": "",
    "app/x/__init__.py": "",
    "app/x/m.py": "from lib import a\n",
    "app/y/__init__.py": "",
    "app/y/n.py": "from lib import a\n",
}


def repo(make_repo: RepoFactory, root_manifest: str, extra: dict[str, str] | None = None) -> Path:
    return make_repo({**LIB, **(extra or {}), "meizel.toml": root_manifest})


def test_a_narrowed_source_covers_only_the_facts_under_it(make_repo: RepoFactory) -> None:
    root = repo(make_repo, '[uses]\n"app/x" = ["lib"]\n')
    assert failures(root) == {("undeclared-edge", "app/y/n.py")}


def test_the_undeclared_message_names_the_narrowed_declarations_of_the_edge(
    make_repo: RepoFactory,
) -> None:
    root = repo(make_repo, '[uses]\n"app/x" = ["lib"]\n')
    [found] = diagnostics(root, "undeclared-edge")
    text = found.render()
    assert "declared only for narrowed endpoints" in text
    assert "app/x -> lib" in text


def test_a_narrowed_target_covers_only_the_facts_into_it(make_repo: RepoFactory) -> None:
    files = {"app/y/n.py": "from lib import b\n"}
    root = repo(make_repo, '[uses]\n"app" = ["lib/a.py"]\n', files)
    assert failures(root) == {("undeclared-edge", "app/y/n.py")}


def test_a_plain_child_name_covers_everything_under_it(make_repo: RepoFactory) -> None:
    root = repo(make_repo, '[uses]\n"app" = ["lib"]\n')
    assert failures(root) == set()


def test_both_ends_can_be_narrowed_and_a_file_is_a_prefix_of_itself(make_repo: RepoFactory) -> None:
    root = repo(make_repo, '[uses]\n"app/x/m.py" = ["lib/a.py"]\n"app/y" = ["lib"]\n')
    assert failures(root) == set()


def test_the_cycle_check_runs_on_the_projected_graph(make_repo: RepoFactory) -> None:
    files = {"lib/b.py": "from app import x\n", "lib/meizel.toml": 'exports = ["a.py", "b.py"]\n'}
    root = repo(make_repo, '[uses]\n"app/x" = ["lib/a.py"]\n"lib/b.py" = ["app/x"]\n', files)
    [found] = diagnostics(root, "manifest-cycle")
    assert "app -> lib -> app" in found.message or "lib -> app -> lib" in found.message


def test_a_narrowed_declaration_no_fact_matches_is_stale(make_repo: RepoFactory) -> None:
    root = repo(
        make_repo,
        '[uses]\n"app/x" = ["lib"]\n"app/y" = ["lib/b.py"]\n"app/y/n.py" = ["lib/a.py"]\n',
    )
    stale = diagnostics(root, "manifest-stale-edge")
    assert {d.message.split(" is declared")[0] for d in stale} == {"app/y -> lib/b.py"}


def test_prune_removes_only_the_stale_narrowed_value(make_repo: RepoFactory) -> None:
    root = repo(make_repo, '[uses]\n"app/x" = ["lib"]\n"app/y" = ["lib/a.py", "lib/b.py"]\n')
    assert run(root, "sync", "--prune") == 0
    parsed = tomllib.loads((root / "meizel.toml").read_text())
    assert parsed["uses"] == {"app/x": ["lib"], "app/y": ["lib/a.py"]}


def test_a_narrowed_endpoint_must_exist_and_cannot_stay_inside_one_child(
    make_repo: RepoFactory,
) -> None:
    root = repo(make_repo, '[uses]\n"app/x" = ["lib/ghost.py"]\n"app/y" = ["app/x"]\n')
    codes = {d.code.value for d in analysis_of(root).diagnostics}
    assert {"manifest-unknown-child", "manifest-self-edge"} <= codes


def test_exports_along_the_path_still_apply(make_repo: RepoFactory) -> None:
    files = {"lib/meizel.toml": 'exports = ["b.py"]\n'}
    root = repo(make_repo, '[uses]\n"app" = ["lib"]\n', files)
    assert ("not-exported", "app/x/m.py") in failures(root)


def test_another_service_importing_the_kernel_is_an_error_while_the_one_exception_passes(
    make_repo: RepoFactory,
) -> None:
    files = {
        "agent/__init__.py": "",
        "agent/db.py": "",
        "agent/graph.py": "",
        "agent/meizel.toml": 'exports = ["db.py", "graph.py"]\n',
        "services/__init__.py": "",
        "services/host/__init__.py": "",
        "services/host/daemon.py": "from agent import db\n",
        "services/other/__init__.py": "",
        "services/other/daemon.py": "from agent import db\n",
        "meizel.toml": '[uses]\n"services/host" = ["agent/db.py"]\n',
    }
    assert failures(make_repo(files)) == {("undeclared-edge", "services/other/daemon.py")}


def declare(root: Path, *paths: str) -> int:
    code = run(root, "sync", "--declare", *paths)
    git(root, "add", "-A")
    return code


def test_declare_extends_the_targets_of_an_authorised_source(make_repo: RepoFactory) -> None:
    files = {"app/x/m.py": "from lib import a, b\n"}
    root = repo(make_repo, '[uses]\n"app/x" = ["lib/a.py"]\n"app/y" = ["lib"]\n', files)
    assert declare(root, "app/x") == 0
    parsed = tomllib.loads((root / "meizel.toml").read_text())
    assert parsed["uses"]["app/x"] == ["lib/a.py", "lib/b.py"]
    assert failures(root) == set()


def test_declare_never_authorises_a_new_source_of_a_narrowed_edge(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(make_repo, '[uses]\n"app/x" = ["lib"]\n')
    before = (root / "meizel.toml").read_text()
    assert declare(root, "app/y") == 1
    assert (root / "meizel.toml").read_text() == before
    assert "declared only for narrowed endpoints" in capsys.readouterr().out
