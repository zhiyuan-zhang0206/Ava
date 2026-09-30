"""Where pytest looks for tests: the top-level `tests/` and every package's own `tests/`.

A test lives in a `tests/` directory, either the top-level one (e2e, contract and
shared-support tests) or `<pkg>/**/tests/` beside the code it proves. Bare `pytest`
and CI's backend shards (which pass no positional path) collect `testpaths` from
pyproject.toml, expanded as recursive globs. A tests directory that no glob
reaches is never collected and pytest says nothing, so completeness is asserted
here rather than trusted.
"""

from __future__ import annotations

import glob
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
_PYTEST = _PYPROJECT["tool"]["pytest"]["ini_options"]

# Directories a `tests/` directory may live under (mirrors `testpaths`).
_HOSTS = (
    "tests",
    "agent",
    "ava",
    "ava_builtins",
    "base",
    "cli",
    "gateway",
    "ops",
    "scripts",
    "services",
)


def _tracked(pattern: str) -> list[str]:
    result = subprocess.run(  # noqa: S603 — fixed git query
        ["git", "-C", str(_REPO_ROOT), "ls-files", "-z", "--", pattern],
        capture_output=True,
        text=True,
        check=True,
    )
    return [path for path in result.stdout.split("\0") if path]


def _resolved_roots() -> list[str]:
    """The directories pytest visits for a bare invocation, expanded as it expands them."""
    roots: list[str] = []
    for pattern in _PYTEST["testpaths"]:
        # pytest expands `testpaths` with glob.iglob itself; mirror it exactly.
        roots.extend(sorted(glob.iglob(pattern, root_dir=_REPO_ROOT, recursive=True)))  # noqa: PTH207
    return roots


def test_every_tracked_test_module_is_under_a_collected_root() -> None:
    roots = tuple(f"{root}/" for root in _resolved_roots())
    modules = [
        path
        for path in _tracked("*.py")
        if path.split("/")[0] in _HOSTS
        and "tests" in path.split("/")[:-1]
        and Path(path).name.startswith("test_")
    ]
    assert modules, "no test modules found: the discovery itself is broken"
    orphans = sorted(path for path in modules if not path.startswith(roots))
    assert not orphans, (
        "test modules outside every `testpaths` glob (pytest would never collect them): "
        f"{orphans[:10]}"
    )


def test_a_collected_root_is_not_inside_another() -> None:
    """pytest 9 binds a conftest to the first Directory node of a directory: nested roots split it."""
    roots = _resolved_roots()
    nested = sorted(
        (inner, outer)
        for inner in roots
        for outer in roots
        if inner != outer and inner.startswith(f"{outer}/")
    )
    assert not nested, f"nested collection roots: {nested}"


def test_every_glob_only_names_a_directory_that_exists_or_may_exist() -> None:
    """A glob is `<host>/**/tests` or the literal `tests`; a typo names no host directory."""
    for pattern in _PYTEST["testpaths"]:
        host, _, tail = pattern.partition("/")
        assert host in _HOSTS, pattern
        assert tail in ("", "**/tests"), pattern
        assert (_REPO_ROOT / host).is_dir(), pattern


def test_python_files_makes_location_the_test_definition(tmp_path: Path) -> None:
    """A whole-repo collection must not import production modules named `test_*.py`.

    `base/db/test_db_guard.py` and `scripts/ci/test_selector.py` are production
    modules. The real `testpaths` and `python_files` are applied to a scratch tree:
    bare `pytest` must collect exactly the test modules inside `tests/` directories.
    """
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        f"testpaths = {_PYTEST['testpaths']!r}\n"
        f"python_files = {_PYTEST['python_files']!r}\n"
        'addopts = ["--import-mode=importlib"]\n',
        encoding="utf-8",
    )
    test_module = "def test_it() -> None:\n    pass\n"
    for rel in (
        "tests/test_top.py",
        "tests/agent/test_nested.py",
        "base/pkg/tests/test_a.py",
        "base/pkg/tests/area/test_b.py",
        "ava_builtins/skills/x/scripts/tests/test_c.py",
        "services/pitr/stores/cos/tests/test_d.py",
        # Production modules that happen to be named test_*.py.
        "base/db/test_db_guard.py",
        "scripts/ci/test_selector.py",
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(test_module, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    collected = sorted(line for line in result.stdout.splitlines() if "::" in line)
    assert collected == [
        "ava_builtins/skills/x/scripts/tests/test_c.py::test_it",
        "base/pkg/tests/area/test_b.py::test_it",
        "base/pkg/tests/test_a.py::test_it",
        "services/pitr/stores/cos/tests/test_d.py::test_it",
        "tests/agent/test_nested.py::test_it",
        "tests/test_top.py::test_it",
    ], result.stdout + result.stderr


def test_a_module_named_test_outside_a_tests_directory_is_not_a_repo_test() -> None:
    """The two known production modules named `test_*.py`, so a new one is noticed here."""
    outside = sorted(
        path
        for path in _tracked("*.py")
        if Path(path).name.startswith("test_") and "tests" not in path.split("/")[:-1]
    )
    assert outside == ["base/db/test_db_guard.py", "scripts/ci/test_selector.py"]


@pytest.mark.parametrize("step", ["Run pytest shard", "Run flaky pytest bucket serially"])
def test_ci_backend_commands_collect_from_testpaths(step: str) -> None:
    """The shards and the flaky bucket name no positional path, so they follow `testpaths`."""
    workflow = yaml.safe_load((_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    commands = [
        candidate["run"]
        for job in workflow["jobs"].values()
        for candidate in job.get("steps", [])
        if candidate.get("name") == step
    ]
    assert len(commands) == 1
    assert "uv run pytest" in commands[0]
    assert "pytest tests/" not in commands[0]
