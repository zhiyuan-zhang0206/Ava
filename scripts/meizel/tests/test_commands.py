"""The four commands, end to end on throwaway repositories."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.meizel.cli import main
from scripts.meizel.sync import remove_entries
from scripts.meizel.tests.synthetic import RepoFactory, verdict

SOURCES = {
    "pkg/__init__.py": "",
    "pkg/a.py": "from pkg import b\n",
    "pkg/b.py": "VALUE = 1\n",
    "app/__init__.py": "",
    "app/run.py": "from pkg import a\n",
}
MANIFESTS = {
    "pkg/meizel.toml": 'exports = ["a.py"]\n\n[uses]\n"a.py" = ["b.py"]\n',
    "meizel.toml": '[uses]\n"app" = ["pkg"]\n',
}


def run(root: Path, *args: str) -> int:
    return main(["--repo", str(root), "--jobs", "1", *args])


def test_check_passes_a_conforming_repository(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo({**SOURCES, **MANIFESTS})
    assert run(root, "check") == 0
    assert "meizel: ok" in capsys.readouterr().out


def test_check_fails_with_file_line_edge_directory_and_fix(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(SOURCES)
    assert run(root, "check") == 1
    out = capsys.readouterr().out
    assert "pkg/a.py:1: [undeclared-edge] pkg/a.py -> pkg/b.py" in out
    assert "L = pkg" in out
    assert '"a.py" = [..., "b.py"]' in out
    assert "app/run.py:1: [undeclared-edge] app/run.py -> pkg/a.py" in out


def test_check_with_paths_reports_only_diagnostics_under_them(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(SOURCES)
    assert run(root, "check", "app") == 1
    out = capsys.readouterr().out
    assert "app/run.py" in out
    assert "pkg/a.py:1" not in out
    assert run(root, "check", "pkg/b.py") == 0


def test_check_rejects_an_unknown_path(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(SOURCES)
    assert run(root, "check", "nowhere") == 2
    assert "nowhere" in capsys.readouterr().err


def test_check_limit_caps_the_printout_not_the_verdict(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(SOURCES)
    assert run(root, "check", "--limit", "1") == 1
    captured = capsys.readouterr()
    assert captured.out.count(": [") == 1
    assert "not shown" in captured.err


def test_outside_a_git_checkout_the_tool_stops(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, "check") == 2
    assert "git ls-files failed" in capsys.readouterr().err


def test_unparsable_source_stops_the_tool_with_its_location(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo({"pkg/__init__.py": "", "pkg/bad.py": "def f(:\n"})
    assert run(root, "check") == 2
    assert "pkg/bad.py:1" in capsys.readouterr().err


def test_explain_a_file_shows_its_place_in_the_parent_graph(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo({**SOURCES, **MANIFESTS})
    assert run(root, "explain", "pkg/a.py") == 0
    out = capsys.readouterr().out
    assert "pkg/a.py (file)" in out
    assert "declared to use: b.py" in out
    assert "exported by parent: yes" in out
    assert "pkg/b.py" in out
    assert "app/run.py:1 import" in out


def test_explain_a_directory_shows_exports_and_its_local_graph(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo({**SOURCES, **MANIFESTS})
    assert run(root, "explain", "pkg") == 0
    out = capsys.readouterr().out
    assert "pkg (directory)" in out
    assert "exports: a.py" in out
    assert "a.py -> b.py" in out
    assert "declared to be used by: app" in out


def test_explain_rejects_an_unknown_node(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(make_repo(SOURCES), "explain", "ghost.py") == 2


def test_prune_removes_only_the_stale_declarations(make_repo: RepoFactory) -> None:
    manifest = (
        "# why this directory looks like this\n"
        'exports = ["a.py"]\n\n'
        "[uses]\n"
        '"a.py" = ["b.py"]\n'
        '"__init__.py" = ["a.py", "b.py"]  # stale\n'
        '"b.py" = [\n    "__init__.py",\n]\n'
    )
    root = make_repo(
        {**SOURCES, "pkg/meizel.toml": manifest, "meizel.toml": MANIFESTS["meizel.toml"]}
    )
    assert run(root, "sync", "--prune") == 0
    text = (root / "pkg/meizel.toml").read_text()
    assert (
        text
        == '# why this directory looks like this\nexports = ["a.py"]\n\n[uses]\n"a.py" = ["b.py"]\n'
    )
    assert verdict(root) == set()


def test_prune_cascades_through_a_stale_load_and_deletes_an_emptied_manifest(
    make_repo: RepoFactory,
) -> None:
    files = {
        "lib/__init__.py": "",
        "app/__init__.py": "",
        "app/loader.py": "x = 1\n",
        "app/meizel.toml": '[loads]\n"loader.py" = ["lib"]\n',
        "meizel.toml": '[uses]\n"app" = ["lib"]\n',
    }
    root = make_repo(files)
    assert run(root, "sync", "--prune") == 0
    assert not (root / "app/meizel.toml").exists()
    assert not (root / "meizel.toml").exists()
    assert verdict(root) == set()


def test_remove_entries_handles_multiline_values_and_keeps_other_sections() -> None:
    text = '[uses]\n"a" = [\n    "x",\n    "y",\n]\n"b" = ["z"]\n\n[loads]\n"l.py" = ["d"]\n'
    out = remove_entries(text, {"uses": {"a": {"x"}, "b": {"z"}}})
    assert out == '[uses]\n"a" = ["y"]\n\n[loads]\n"l.py" = ["d"]\n'


def test_bootstrap_writes_a_mirror_that_passes_check_and_leaves_the_repo_alone(
    make_repo: RepoFactory, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(SOURCES)
    out = tmp_path / "out"
    assert run(root, "bootstrap", "--out", str(out)) == 0
    assert (
        out / "pkg/meizel.toml"
    ).read_text() == 'exports = ["a.py"]\n\n[uses]\n"a.py" = ["b.py"]\n'
    assert (out / "meizel.toml").read_text() == '[uses]\n"app" = ["pkg"]\n'
    assert not list(root.rglob("meizel.toml"))
    report = json.loads((out / "report.json").read_text())
    assert report["summary"]["cyclic_directories"] == 0
    assert report["summary"]["local_edges"] == 2
    for manifest in out.rglob("meizel.toml"):
        shutil.copy(manifest, root / manifest.relative_to(out))
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)  # noqa: S603 - fixed argv
    assert verdict(root) == set()
    capsys.readouterr()


CYCLE = {
    "pkg/__init__.py": "",
    "pkg/a.py": "from pkg import b\n",
    "pkg/b.py": "from pkg import c\n",
    "pkg/c.py": "from pkg import a\n",
    "pkg/tests/test_a.py": "from pkg import a\n",
}


def test_bootstrap_reports_cycles_with_a_witness_fact_per_edge(
    make_repo: RepoFactory, tmp_path: Path
) -> None:
    root = make_repo(CYCLE)
    assert run(root, "bootstrap", "--out", str(tmp_path / "out")) == 0
    report = json.loads((tmp_path / "out/report.json").read_text())
    assert report["cyclic_directories"] == report["cyclic_directories_production_only"] == ["pkg"]
    [component] = report["directories"]["pkg"]["components"]
    assert component["members"] == ["a.py", "b.py", "c.py"]
    assert len(component["cycle"]) == 4
    assert {w["fact"].split(":")[0] for w in component["witnesses"]} == {
        "pkg/a.py",
        "pkg/b.py",
        "pkg/c.py",
    }
    assert report["directories"]["pkg"]["edges"] == 4


def test_bootstrap_separates_production_cycles_from_cycles_that_need_tests(
    make_repo: RepoFactory, tmp_path: Path
) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "from pkg.tests import helpers\n",
        "pkg/tests/helpers.py": "from pkg import a\n",
    }
    root = make_repo(files)
    assert run(root, "bootstrap", "--out", str(tmp_path / "out")) == 0
    report = json.loads((tmp_path / "out/report.json").read_text())
    assert report["cyclic_directories"] == ["pkg"]
    assert report["cyclic_directories_production_only"] == []


@pytest.mark.parametrize("command", [["check"], ["explain", "pkg"], ["sync", "--prune"]])
def test_commands_run_from_the_cwd_when_no_repo_is_given(
    make_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    root = make_repo({**SOURCES, **MANIFESTS})
    monkeypatch.chdir(root)
    assert main(["--jobs", "1", *command]) == 0
