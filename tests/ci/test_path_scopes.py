"""Locks tests/fixtures/path_scopes.py: directory-level fixtures that follow their tests.

The plugin registers a fixture module for a directory or test file the way pytest
registers a conftest, through `FixtureManager.parsefactories(holder=, node=)`. These
tests pin that interface, the conftest-identical behavior (autouse names and their
order, a session-scoped autouse fixture instantiated only for governed tests,
subset runs, interleaved paths), and the two ways a table goes stale: a path that
no longer exists, and a listed directory that no longer holds a test.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _pytest.fixtures import FixtureManager

from tests.fixtures import path_scopes
from tests.fixtures.path_scopes import PATH_SCOPES, Scope, discover_scopes, scope_problems

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SCOPED = """
import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _sess():
    Path(os.environ["SESSION_MARK"]).write_text("ran")


@pytest.fixture(autouse=True)
def _func():
    return None


@pytest.fixture
def opt_in():
    return "scoped"
"""
# The plugin the way the repo loads it: named by `pytest_plugins` in a root conftest (hooks
# defined in a conftest itself are not called for every directory node).
_PLUGIN = """
from tests.fixtures import path_scopes as ps

ps._MODULES_BY_PATH.clear()
ps._MODULES_BY_PATH.update(ps.modules_by_path({scopes}))
pytest_configure = ps.pytest_configure
pytest_collectstart = ps.pytest_collectstart
"""
_GOVERNED = """
def test_{name}(request, opt_in):
    names = request.fixturenames
    assert opt_in == "scoped"
    assert names.index("_sess") < names.index("_func")
"""
_FREE = """
def test_{name}(request):
    assert "_sess" not in request.fixturenames
    assert "_func" not in request.fixturenames
"""


def _tree(root: Path, scopes: dict[str, Scope]) -> None:
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "scoped_mod.py").write_text(_SCOPED, encoding="utf-8")
    listed = ", ".join(f"{module!r}: ps.Scope({scope.paths!r})" for module, scope in scopes.items())
    (root / "conftest.py").write_text('pytest_plugins = ["scoped_plugin"]\n', encoding="utf-8")
    (root / "scoped_plugin.py").write_text(
        _PLUGIN.format(scopes="{" + listed + "}"), encoding="utf-8"
    )
    for rel, template in {
        "gov/test_a.py": _GOVERNED,
        "gov/test_b.py": _GOVERNED,
        "one/test_c.py": _GOVERNED,
        "one/test_d.py": _FREE,
        "free/test_e.py": _FREE,
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.format(name=path.stem[5:]), encoding="utf-8")


def _scopes() -> dict[str, Scope]:
    return {"scoped_mod": Scope(("gov", "one/test_c.py"))}


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env["SESSION_MARK"] = str(root / "session.mark")
    env["PYTHONPATH"] = os.pathsep.join([str(root), str(_REPO_ROOT)])
    return subprocess.run(  # noqa: S603 — our own interpreter, synthetic tmp paths
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_pytest_still_offers_the_registration_interface_the_plugin_uses() -> None:
    """A pytest upgrade that changes this makes the plugin unusable: re-check it then."""
    parameters = inspect.signature(FixtureManager.parsefactories).parameters
    for name in ("holder", "node"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name


def test_governed_tests_get_the_fixtures_a_conftest_would_give(tmp_path: Path) -> None:
    _tree(tmp_path, _scopes())
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "5 passed" in proc.stdout, proc.stdout[-2000:]


def test_a_session_scoped_autouse_fixture_is_instantiated_only_for_governed_tests(
    tmp_path: Path,
) -> None:
    _tree(tmp_path, _scopes())
    proc = _run(tmp_path, "free/test_e.py", "one/test_d.py")
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert not (tmp_path / "session.mark").exists()
    proc = _run(tmp_path, "gov/test_a.py::test_a")
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert (tmp_path / "session.mark").exists()


def test_subset_runs_keep_the_fixtures(tmp_path: Path) -> None:
    _tree(tmp_path, _scopes())
    for args in (("-k", "test_b", "gov"), ("gov/test_b.py::test_b",), ("one/test_c.py",)):
        proc = _run(tmp_path, *args)
        assert proc.returncode == 0, (args, proc.stdout[-2000:])
        assert "1 passed" in proc.stdout, (args, proc.stdout[-2000:])


def test_paths_that_leave_a_directory_and_come_back_keep_the_fixtures(tmp_path: Path) -> None:
    """Each collector node of a directory is registered, so a revisited one is not left bare."""
    _tree(tmp_path, _scopes())
    proc = _run(tmp_path, "gov/test_a.py", "free/test_e.py", "gov/test_b.py")
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert "3 passed" in proc.stdout, proc.stdout[-2000:]


def test_a_listed_path_that_does_not_exist_stops_the_run(tmp_path: Path) -> None:
    _tree(tmp_path, {"scoped_mod": Scope(("gov", "moved_away"))})
    proc = _run(tmp_path)
    assert proc.returncode != 0
    assert "do not exist" in proc.stdout + proc.stderr
    assert "moved_away" in proc.stdout + proc.stderr


def test_the_recorded_table_matches_the_tree() -> None:
    assert scope_problems(PATH_SCOPES, _REPO_ROOT) == []
    for module in PATH_SCOPES:
        importlib.import_module(module)


def test_a_directory_left_without_tests_is_reported(tmp_path: Path) -> None:
    """The stale entry a directory leaves behind once its last test moved out."""
    (tmp_path / "gov").mkdir()
    (tmp_path / "gov" / "test_a.py").write_text("", encoding="utf-8")
    scopes = {"scoped_mod": Scope(("gov",))}
    assert scope_problems(scopes, tmp_path) == []
    (tmp_path / "gov" / "test_a.py").rename(tmp_path / "test_a.py")
    problems = scope_problems(scopes, tmp_path)
    assert len(problems) == 1, problems
    assert "hold no test file" in problems[0]
    assert scope_problems({"scoped_mod": Scope(("test_a.py",))}, tmp_path) == []


def test_scope_validity_does_not_prove_a_moved_tests_autouse_closure(tmp_path: Path) -> None:
    """Deleting the old declaration can hide a move's missing autouse environment.

    This characterizes the scope validator's boundary, not a migration gate:
    runtime fixture evidence must accompany a move even when every path is valid.
    """
    _tree(tmp_path, {})
    (tmp_path / "scoped_plugin.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "from tests.fixtures import path_scopes as ps\n"
        "ps._MODULES_BY_PATH.clear()\n"
        "ps._MODULES_BY_PATH.update(ps.modules_by_path(ps.discover_scopes(Path.cwd())))\n"
        "pytest_configure = ps.pytest_configure\n"
        "pytest_collectstart = ps.pytest_collectstart\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    if call.when == 'call':\n"
        "        resolved = {name: f'{definition.func.__module__}:'\n"
        "                    f'{definition.func.__qualname__}:{definition.scope}'\n"
        "                    for name, definition in item._request._fixture_defs.items()}\n"
        "        Path('fixture-closure.json').write_text(json.dumps(resolved))\n",
        encoding="utf-8",
    )
    original = tmp_path / "gov" / "test_hidden.py"
    original.write_text("def test_hidden():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    original_scope = original.parent / "path_scopes.toml"
    declaration = '"scoped_mod" = ["test_hidden.py"]\n'
    original_scope.write_text(declaration, encoding="utf-8")
    marker = tmp_path / "session.mark"
    receipt = tmp_path / "fixture-closure.json"

    before = _run(tmp_path, "gov/test_hidden.py")
    assert before.returncode == 0, before.stdout + before.stderr
    assert marker.exists()
    original_closure = json.loads(receipt.read_text(encoding="utf-8"))
    receipt.rename(tmp_path / "fixture-closure-before.json")
    assert original_closure["_sess"] == "scoped_mod:_sess:session"
    assert original_closure["_func"] == "scoped_mod:_func:function"

    destination = tmp_path / "moved" / "test_hidden.py"
    destination.parent.mkdir()
    original.rename(destination)
    original_scope.unlink()
    marker.unlink()
    assert scope_problems(discover_scopes(tmp_path), tmp_path) == []
    omitted = _run(tmp_path, "moved/test_hidden.py")
    assert omitted.returncode == 0, omitted.stdout + omitted.stderr
    assert "1 passed" in omitted.stdout
    assert not marker.exists()
    omitted_closure = json.loads(receipt.read_text(encoding="utf-8"))
    receipt.rename(tmp_path / "fixture-closure-omitted.json")
    assert "_sess" not in omitted_closure
    assert "_func" not in omitted_closure

    (destination.parent / "path_scopes.toml").write_text(declaration, encoding="utf-8")
    repaired = _run(tmp_path, "moved/test_hidden.py")
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert marker.exists()
    assert json.loads(receipt.read_text(encoding="utf-8")) == original_closure


def test_the_plugin_registers_only_listed_paths() -> None:
    by_path = path_scopes.modules_by_path(PATH_SCOPES)
    assert set(by_path) == {p for scope in PATH_SCOPES.values() for p in scope.paths}
    for path, modules in by_path.items():
        declared = {module for module, scope in PATH_SCOPES.items() if path in scope.paths}
        assert set(modules) == declared
        assert len(modules) == len(declared)


def test_scopes_are_read_from_the_files_next_to_the_tests(tmp_path: Path) -> None:
    """A declaration lives in the directory it governs: `"."` is that directory, a name is a
    test file in it; modules merge across files and come out alphabetically."""
    (tmp_path / "pkg" / "tests").mkdir(parents=True)
    (tmp_path / "pkg" / "tests" / "path_scopes.toml").write_text(
        '"mod_b" = ["."]\n"mod_a" = ["test_one.py", "test_two.py"]\n', encoding="utf-8"
    )
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "path_scopes.toml").write_text('"mod_a" = ["."]\n', encoding="utf-8")
    for ignored in (".venv", "node_modules", ".worktrees/x"):
        (tmp_path / ignored).mkdir(parents=True)
        (tmp_path / ignored / "path_scopes.toml").write_text('"mod_z" = ["."]\n', encoding="utf-8")

    assert discover_scopes(tmp_path) == {
        "mod_a": Scope(("other", "pkg/tests/test_one.py", "pkg/tests/test_two.py")),
        "mod_b": Scope(("pkg/tests",)),
    }


def test_a_scope_file_that_does_not_list_names_is_refused(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "path_scopes.toml").write_text('"mod_a" = "."\n', encoding="utf-8")
    with pytest.raises(ValueError, match="must be a list of names"):
        discover_scopes(tmp_path)


def test_the_plugin_module_holds_no_central_listing() -> None:
    """The declarations stay next to the tests: nothing in the plugin names a test path."""
    source = (_REPO_ROOT / "tests" / "fixtures" / "path_scopes.py").read_text(encoding="utf-8")
    reader = (_REPO_ROOT / "scripts/structure/imports/fixture_scopes.py").read_text(
        encoding="utf-8"
    )
    assert "tests/" not in reader.split("def modules_by_path")[1]
    # Exclude the descriptive module docstring; inspect executable declarations.
    plugin = ast.parse(source)
    body = plugin.body[1:] if ast.get_docstring(plugin) is not None else plugin.body
    declarations = ast.Module(body=body, type_ignores=[])
    assert not any(
        isinstance(node, ast.Constant) and isinstance(node.value, str) and "tests/" in node.value
        for node in ast.walk(declarations)
    )
