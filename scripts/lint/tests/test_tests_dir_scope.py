"""A package's own `tests/` directory under the structure gate.

Tests live either in the top-level `tests/` or beside the code they prove, in
`<pkg>/**/tests/`. Every rule that treats `tests/` specially must treat both
places alike, or the tests that move into a package silently change what the
gate does to them: strict budgets and the AST rules.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess

import pytest

from scripts.lint import code_structure as lcs
from scripts.structure import baseline_shards, path_imports

_SECTIONS = lcs._SITE_SECTIONS


@pytest.fixture(autouse=True)
def _isolated_repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each main() call scans only its own temporary root, with an empty baseline."""
    monkeypatch.setattr(lcs, "_REPO_ROOT", tmp_path)
    monkeypatch.setenv("LINT_STRUCTURE_BASELINE_BASE", "HEAD")
    _write_baseline(tmp_path, {section: {} for section in _SECTIONS})
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", baseline_shards.SHARD_DIR)
    _git(tmp_path, "commit", "--quiet", "-m", "Empty baseline")


def _write_baseline(root: pathlib.Path, data: dict[str, dict[str, int]]) -> None:
    directory = root / baseline_shards.SHARD_DIR
    if directory.is_dir():
        for path in directory.rglob("*.json"):
            path.unlink()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "README.md").write_text("Structure baseline shards.\n", encoding="utf-8")
    for name, shard in baseline_shards.split(data).items():
        pathlib.Path(f"{directory}/{name}.json").parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(f"{directory}/{name}.json").write_text(
            baseline_shards.render(shard), encoding="utf-8"
        )


def _module(path: pathlib.Path, body: str = "x = 1\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _fill(directory: pathlib.Path, count: int) -> None:
    for index in range(count):
        _module(directory / f"entry_{index}.py")


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 — fixed test commands, never external input
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Structure gate test",
            "-c",
            "user.email=structure-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


# ── budgets ─────────────────────────────────────────────────────────────────


def test_tests_layer_takes_a_slot_in_its_parent(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Tests take one parent slot even without an __init__.py."""
    package = tmp_path / "base/pkg"
    _fill(package, 20)
    _module(package / "tests/test_pkg.py")
    _git(tmp_path, "add", "base/pkg")
    assert lcs.main([]) == 1
    assert "base/pkg: directory has 21 direct entries" in capsys.readouterr().out


def test_tests_package_with_init_takes_a_slot(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `tests` directory with `__init__.py` is a real Python package: budgeted like code."""
    package = tmp_path / "base/pkg"
    _fill(package, 20)
    _module(package / "tests/__init__.py")
    _git(tmp_path, "add", "base/pkg")
    assert lcs.main([]) == 1
    assert "base/pkg: directory has 21 direct entries" in capsys.readouterr().out


@pytest.mark.parametrize("location", ["tests", "base/pkg/tests", "ava_builtins/skills/x/tests"])
def test_tests_layer_has_the_same_entry_cap(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], location: str
) -> None:
    """Test directories obey the same limit at the root and inside packages."""
    _fill(tmp_path / location, 21)
    _git(tmp_path, "add", location)
    assert lcs.main([]) == 1
    assert f"{location}: directory has 21 direct entries" in capsys.readouterr().out


def test_directory_below_a_tests_layer_is_still_capped(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fill(tmp_path / "base/pkg/tests/area", 21)
    _git(tmp_path, "add", "base/pkg/tests")
    assert lcs.main([]) == 1
    assert "base/pkg/tests/area: directory has 21 direct entries" in capsys.readouterr().out


def test_tests_package_with_init_keeps_its_own_cap(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _module(tmp_path / "base/pkg/tests/__init__.py")
    _fill(tmp_path / "base/pkg/tests", 21)
    _git(tmp_path, "add", "base/pkg/tests")
    assert lcs.main([]) == 1
    assert "base/pkg/tests: directory has 22 direct entries" in capsys.readouterr().out


def test_test_files_keep_the_line_ceiling(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _module(tmp_path / "base/pkg/tests/test_big.py", "x = 1\n" * 801)
    assert lcs.main([]) == 1
    assert "base/pkg/tests/test_big.py:801: file is 801 lines" in capsys.readouterr().out


# ── AST rules ───────────────────────────────────────────────────────────────

_BREAKS_EVERY_AST_RULE = """\
import sys
from typing import TYPE_CHECKING

from base.cluster.machine import machine_role
from base.other._private import hidden

if TYPE_CHECKING:
    import decimal

sys.path.insert(0, "/somewhere")
machine_role()
hidden()
"""


def _private_owner(root: pathlib.Path) -> None:
    """A real package with a private module, so Rule 4 has something to reach into."""
    _module(root / "base/other/__init__.py")
    _module(root / "base/other/_private.py", "def hidden() -> None: ...\n")


@pytest.mark.parametrize(
    "location",
    [
        "base/pkg/tests/test_x.py",
        "gateway/agents/tests/test_x.py",
        "ava_builtins/plugins/p/tests/test_x.py",
    ],
)
def test_ast_rules_do_not_govern_a_package_tests_directory(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], location: str
) -> None:
    _private_owner(tmp_path)
    _module(tmp_path / location, _BREAKS_EVERY_AST_RULE)
    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("location", ["base/pkg/mod.py", "ava_builtins/plugins/p/run.py"])
def test_the_same_source_outside_tests_is_governed(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], location: str
) -> None:
    """The control: the exemption is the location, not a weakened rule."""
    _private_owner(tmp_path)
    _module(tmp_path / location, _BREAKS_EVERY_AST_RULE)
    assert lcs.main([]) == 1
    out = capsys.readouterr().out
    assert "`if TYPE_CHECKING:` is banned" in out
    assert "machine_role() may only be called" in out
    assert "reaches private `base.other._private`" in out  # Rule 4


def test_path_import_rule_skips_test_files_at_any_depth() -> None:
    tree = ast.parse('import sys\nsys.path.insert(0, "/somewhere")\n')
    assert (
        path_imports.measure(
            tree, "ava_builtins/skills/integrations/gmail/scripts/tests/test_gmail.py"
        )
        == {}
    )
    assert (
        path_imports.measure(tree, "ava_builtins/tests/schedules/test_goal_watch_filter.py") == {}
    )
    assert path_imports.measure(tree, "ava_builtins/skills/integrations/gmail/scripts/run.py") == {
        "ava_builtins/skills/integrations/gmail/scripts/run.py::sys.path": [2]
    }


def test_test_module_move_still_enforces_file_and_function_budgets(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old, new = "tests/components/agent/test_big.py", "agent/graph/tests/test_big.py"
    body = "def f(x):\n" + "    if x: pass\n" * 15 + "    return x\n" + "y = 1\n" * 800
    _module(tmp_path / old, body)
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "--quiet", "-m", "Commit an over-budget test module")
    (tmp_path / new).parent.mkdir(parents=True)
    _git(tmp_path, "mv", old, new)

    assert lcs.main([]) == 1
    output = capsys.readouterr().out
    assert f"{new}:817:" in output
    assert f"{new}::f: complexity 16" in output

    (tmp_path / new).write_text("def f(x):\n    return x\n", encoding="utf-8")
    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""
