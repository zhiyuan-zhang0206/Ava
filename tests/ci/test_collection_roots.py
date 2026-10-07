"""Where pytest looks for tests: the top-level `tests/` and every package's own `tests/`.

A test lives in a `tests/` directory, either the top-level one (e2e, contract and
shared-support tests) or `<pkg>/**/tests/` beside the code it proves. Bare `pytest`
and CI's backend shards (which pass no positional path) collect `testpaths` from
pyproject.toml, expanded as recursive globs. A tests directory that no glob
reaches is never collected and pytest says nothing, so completeness is asserted
here rather than trusted.

Being under a root is necessary, not sufficient: pytest also needs the file name
to match `python_files` (a `*_test.py` beside its peers is skipped silently) and
must be willing to recurse into every directory on the way (`build/`, `dist/`,
`venv/`, dot-directories). `_collection_problems` applies pytest's own rules to
every tracked module under a `tests/` directory, in both directions: a test module
that would not be collected, and a non-test module that would be. A synthetic tree
run through real pytest pins the predicate to pytest's behavior.
"""

from __future__ import annotations

import fnmatch
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
    "schedules",
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


def _resolved_roots(root_dir: Path = _REPO_ROOT) -> list[str]:
    """The directories pytest visits for a bare invocation, expanded as it expands them."""
    roots: list[str] = []
    for pattern in _PYTEST["testpaths"]:
        # pytest expands `testpaths` with glob.iglob itself; mirror it exactly.
        roots.extend(sorted(glob.iglob(pattern, root_dir=root_dir, recursive=True)))  # noqa: PTH207
    return roots


# pytest's default `norecursedirs` (pyproject.toml does not override it); the scratch-tree
# test below fails if a pytest upgrade changes the default.
_NORECURSEDIRS = ("*.egg", ".*", "_darcs", "build", "CVS", "dist", "node_modules", "venv", "{arch}")
# A module under a `tests/` directory that carries a test-like name, was looked at by a
# person, and is not a test (name -> why). Empty: nothing is decided yet.
_DECIDED_NOT_TESTS: dict[str, str] = {}


def _fnmatch_ex(pattern: str, rel_path: str) -> bool:
    """pytest's `_pytest.pathlib.fnmatch_ex` for a POSIX path below a synthetic root.

    A pattern without `/` matches the last path component; one with `/` matches the
    whole absolute path, with `*/` prepended, and `*` also matches `/`. So
    `*/tests/*/test_*.py` reaches a file at any depth below `tests/`, and would also
    reach `tests/a/test_dir/helper.py`. The absolute path is synthetic so the verdict
    does not depend on where the checkout lives.
    """
    absolute = f"/repo/{rel_path}"
    if "/" not in pattern:
        return fnmatch.fnmatch(absolute.rsplit("/", 1)[-1], pattern)
    return fnmatch.fnmatch(absolute, f"*/{pattern}")


def _why_not_collected(
    rel_path: str,
    *,
    roots: list[str],
    python_files: list[str],
    norecursedirs: tuple[str, ...] = _NORECURSEDIRS,
) -> str | None:
    """Why a bare `pytest` never collects `rel_path`, or None when it does."""
    root = next((root for root in roots if rel_path.startswith(f"{root}/")), None)
    if root is None:
        return "it is outside every `testpaths` root"
    if not any(_fnmatch_ex(pattern, rel_path) for pattern in python_files):
        return f"it matches none of `python_files` {python_files}"
    # The root itself is explicit; pytest applies `norecursedirs` to what it recurses into.
    for directory in rel_path[len(root) + 1 :].split("/")[:-1]:
        if any(_fnmatch_ex(pattern, directory) for pattern in norecursedirs):
            return f"pytest never recurses into `{directory}/` (`norecursedirs`)"
    return None


def _collection_problems(
    tracked: list[str], *, roots: list[str], python_files: list[str]
) -> list[str]:
    """Every tracked module under a `tests/` directory that pytest treats unlike its name."""
    problems: list[str] = []
    for rel_path in tracked:
        *directories, name = rel_path.split("/")
        if "tests" not in directories:
            continue
        why_not = _why_not_collected(rel_path, roots=roots, python_files=python_files)
        if name.startswith("test_"):
            if why_not:
                problems.append(
                    f"{rel_path}: never collected, {why_not}. Fix the glob or move/rename the file."
                )
        elif why_not is None:
            problems.append(
                f"{rel_path}: collected as a test module although its name does not start "
                "with `test_` (a helper inside a `test_*` directory). Rename the directory."
            )
        elif (
            name.endswith("_test.py") or name.startswith("test")
        ) and rel_path not in _DECIDED_NOT_TESTS:
            problems.append(
                f"{rel_path}: named like a test but never collected. Rename it `test_*.py`, or "
                "rename the helper away from test names (or record the decision in "
                "`_DECIDED_NOT_TESTS`)."
            )
    return problems


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


def test_every_tracked_module_under_a_tests_directory_is_collected_as_its_name_says() -> None:
    """The completeness check: a test that is never collected keeps CI green while proving nothing."""
    tracked = _tracked("*.py")
    assert any("tests" in path.split("/")[:-1] for path in tracked), "discovery is broken"
    problems = _collection_problems(
        tracked, roots=_resolved_roots(), python_files=_PYTEST["python_files"]
    )
    assert not problems, "\n".join(problems)


_SYNTHETIC_TREE = {
    # collected
    "tests/test_top.py": True,
    "tests/a/b/c/d/test_four_levels_down.py": True,
    "base/pkg/tests/test_a.py": True,
    "base/pkg/tests/x/y/z/w/test_deep.py": True,
    "base/pkg/tests/tests/test_tests_in_tests.py": True,
    "ava_builtins/skills/s/scripts/tests/test_s.py": True,
    "base/build/tests/test_root_named_build.py": True,  # an explicit root is not skipped
    # a helper in a `test_*` directory is collected too: `*` spans `/`
    "tests/a/test_dir/helper.py": True,
    # not collected
    "tests/a/e_test.py": False,
    "tests/a/testing.py": False,
    "tests/a/conftest.py": False,
    "base/pkg/tests/x_test.py": False,
    "tests/build/test_in_build.py": False,
    "tests/.hidden/test_dot_dir.py": False,
    "tests/venv/test_v.py": False,
    "base/pkg/tests/my.egg/test_egg.py": False,
    "base/pkg/test_production_module.py": False,
    "tests_extra/test_x.py": False,
}


def test_the_predicate_agrees_with_real_pytest_on_a_synthetic_tree(tmp_path: Path) -> None:
    """Pins `_why_not_collected` to what pytest does, so the lint cannot drift from it."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        f"testpaths = {_PYTEST['testpaths']!r}\n"
        f"python_files = {_PYTEST['python_files']!r}\n"
        'addopts = ["--import-mode=importlib"]\n',
        encoding="utf-8",
    )
    for rel in _SYNTHETIC_TREE:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_it() -> None:\n    pass\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    really_collected = {line.split("::")[0] for line in result.stdout.splitlines() if "::" in line}
    roots = _resolved_roots(tmp_path)
    predicted = {
        rel
        for rel in _SYNTHETIC_TREE
        if _why_not_collected(rel, roots=roots, python_files=_PYTEST["python_files"]) is None
    }
    assert really_collected == {rel for rel, collected in _SYNTHETIC_TREE.items() if collected}, (
        result.stdout + result.stderr
    )
    assert predicted == really_collected


def test_the_completeness_check_reports_each_way_a_module_escapes_collection() -> None:
    roots = ["tests", "base/pkg/tests"]
    python_files = _PYTEST["python_files"]
    tracked = [
        # fine: four levels deep, a conftest, a plain helper
        "tests/a/b/c/d/test_ok.py",
        "base/pkg/tests/conftest.py",
        "base/pkg/tests/helpers.py",
        # escapes: name, directory, root
        "base/pkg/tests/e_test.py",
        "base/pkg/tests/testing.py",
        "tests/build/test_in_build.py",
        "base/other/tests/test_no_root.py",
        # collected although it is not named like a test
        "tests/a/test_dir/helper.py",
    ]
    problems = _collection_problems(tracked, roots=roots, python_files=python_files)
    assert [problem.split(":")[0] for problem in problems] == [
        "base/pkg/tests/e_test.py",
        "base/pkg/tests/testing.py",
        "tests/build/test_in_build.py",
        "base/other/tests/test_no_root.py",
        "tests/a/test_dir/helper.py",
    ]
    assert "outside every `testpaths` root" in problems[3]
    assert "norecursedirs" in problems[2]


def test_a_python_files_glob_that_is_too_narrow_is_reported() -> None:
    """The failure this check exists for: a glob one level short drops the nested tests silently."""
    tracked = ["base/pkg/tests/test_a.py", "base/pkg/tests/area/test_b.py"]
    roots = ["base/pkg/tests"]
    assert _collection_problems(tracked, roots=roots, python_files=_PYTEST["python_files"]) == []
    problems = _collection_problems(tracked, roots=roots, python_files=["*/tests/test_*.py"])
    assert [problem.split(":")[0] for problem in problems] == ["base/pkg/tests/area/test_b.py"]
    assert "python_files" in problems[0]


def test_no_pytest_option_or_conftest_excludes_tracked_tests_from_collection() -> None:
    addopts = _PYTEST["addopts"]
    assert not [arg for arg in addopts if arg.startswith(("--ignore", "--deselect"))], addopts
    excluders = sorted(
        path
        for path in _tracked("*conftest.py")
        if Path(path).name == "conftest.py"
        and any(
            name in (_REPO_ROOT / path).read_text(encoding="utf-8")
            for name in ("collect_ignore", "pytest_ignore_collect")
        )
    )
    assert excluders == [], (
        "a conftest excludes tests from collection: state why in this test's known list"
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
        "services/example/area/tests/test_d.py",
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
        "services/example/area/tests/test_d.py::test_it",
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
