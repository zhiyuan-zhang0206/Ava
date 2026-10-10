"""Per-path owner rules of the PR test selector, and the tracked-tree completeness guard.

The synthetic checkout below is sized so a package subset stays under the 80%
duration guard and a top-level-tests fallback stays under it too, which lets each
test observe the rule it names instead of the guard.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.ci import test_selector
from scripts.ci.test_selector import PathClass

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LINT = "tests/test_lint_scan.py"


def _write(repo_root: Path, relative_path: str, content: str = "") -> None:
    path = repo_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _repo(tmp_path: Path) -> Path:
    """A checkout where `base/lm` owns tests, `ops` and `base/empty` own none."""
    _write(
        tmp_path,
        "pyproject.toml",
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests", "base/**/tests", "cli/**/tests", "scripts/**/tests"]\n',
    )
    files = {
        "conftest.py": 'pytest_plugins = ["tests.fixtures.env", "cli.plugins.guard", "pytester"]\n',
        "tests/fixtures/env.py": "",
        "cli/__init__.py": "",
        "cli/plugins/__init__.py": "",
        "cli/plugins/guard.py": "",
        "cli/plugins/other.py": "",
        "tests/unit/test_a.py": "from base.lm import engine\n\ndef test_a(): pass\n",
        "tests/unit/test_b.py": "def test_b(): pass\n",
        "tests/sub/conftest.py": "",
        "tests/sub/test_c.py": "def test_c(): pass\n",
        "tests/sub/test_d.py": "def test_d(): pass\n",
        _LINT: "def test_scan(): pass\n",
        "tests/e2e/test_browser.py": "def test_browser(): pass\n",
        "tests/e2e/conftest.py": "",
        "tests/e2e/helper.py": "",
        "base/lm/__init__.py": "",
        "base/lm/engine.py": "",
        "base/lm/schema.json": "{}\n",
        "base/lm/deep/inner.py": "",
        "base/lm/tests/test_lm.py": "from base.lm import engine\n\ndef test_lm(): pass\n",
        "base/empty/mod.py": "",
        "base/empty/tests/helper.py": "",
        "ops/worker.py": "",
        "scripts/tests/test_bulk.py": "def test_bulk(): pass\n",
        "ui/web/src/App.tsx": "",
        "ui/web/src/bridge.py": "",
        ".github/workflows/ci.yml": "",
        ".agents/skills/demo/SKILL.md": "",
        ".pre-commit-config.yaml": "",
        ".test_durations.source.json": "{}\n",
        "LICENSE": "",
    }
    for relative_path, content in files.items():
        _write(tmp_path, relative_path, content)
    timings = {
        "tests/unit/test_a.py::test_a": 10.0,
        "tests/unit/test_b.py::test_b": 10.0,
        "tests/sub/test_c.py::test_c": 10.0,
        "tests/sub/test_d.py::test_d": 10.0,
        f"{_LINT}::test_scan": 1.0,
        "base/lm/tests/test_lm.py::test_lm": 5.0,
        "scripts/tests/test_bulk.py::test_bulk": 60.0,
    }
    _write(tmp_path, ".test_durations", json.dumps(timings))
    return tmp_path


def _select(repo_root: Path, *changed: str) -> test_selector.SelectionResult:
    return test_selector.select_tests(list(changed), repo_root=repo_root)


_TOP_LEVEL = (
    "tests/sub/test_c.py",
    "tests/sub/test_d.py",
    _LINT,
    "tests/unit/test_a.py",
    "tests/unit/test_b.py",
)


def test_a_source_runs_its_direct_importers_and_its_package_tests(tmp_path: Path) -> None:
    result = _select(_repo(tmp_path), "base/lm/engine.py")

    assert (result.decision, result.reason) == ("SELECTED", "owner-tests")
    assert result.tests == ("base/lm/tests/test_lm.py", _LINT, "tests/unit/test_a.py")


def test_a_package_source_without_importers_still_runs_its_package_tests(tmp_path: Path) -> None:
    """A file no test imports directly is no longer blind: the package owns it."""
    result = _select(_repo(tmp_path), "base/lm/deep/inner.py")

    assert (result.decision, result.tests) == ("SELECTED", ("base/lm/tests/test_lm.py", _LINT))


def test_a_non_python_file_in_a_package_runs_the_package_tests(tmp_path: Path) -> None:
    result = _select(_repo(tmp_path), "base/lm/schema.json")

    assert (result.decision, result.tests) == ("SELECTED", ("base/lm/tests/test_lm.py", _LINT))


@pytest.mark.parametrize("path", ["base/empty/mod.py", "ops/worker.py", "ui/web/src/bridge.py"])
def test_a_package_without_collectable_tests_falls_back_to_the_top_level_tests(
    tmp_path: Path, path: str
) -> None:
    """A `tests/` directory holding only helpers is not an owner; the walk goes on."""
    result = _select(_repo(tmp_path), path)

    assert (result.decision, result.tests) == ("SELECTED", _TOP_LEVEL)


def test_a_subdirectory_conftest_selects_only_its_subtree(tmp_path: Path) -> None:
    result = _select(_repo(tmp_path), "tests/sub/conftest.py")

    assert (result.decision, result.reason) == ("SELECTED", "owner-tests")
    assert result.tests == ("tests/sub/test_c.py", "tests/sub/test_d.py", _LINT)


def test_a_deleted_conftest_still_selects_its_subtree(tmp_path: Path) -> None:
    """Deleting a conftest drops fixtures without breaking any import."""
    repo_root = _repo(tmp_path)
    (repo_root / "tests/sub/conftest.py").unlink()

    result = _select(repo_root, "tests/sub/conftest.py")

    assert result.tests == ("tests/sub/test_c.py", "tests/sub/test_d.py", _LINT)


@pytest.mark.parametrize(
    "path",
    [
        "conftest.py",
        "tests/fixtures/env.py",
        "cli/plugins/guard.py",  # listed in the root conftest's pytest_plugins
        "cli/plugins/__init__.py",  # the plugin's package inits run in every process
        "cli/__init__.py",
    ],
)
def test_global_paths_keep_the_full_suite(tmp_path: Path, path: str) -> None:
    result = _select(_repo(tmp_path), path)

    assert (result.decision, result.reason) == ("FULL", f"global-path:{path}")
    assert result.forced_roots == (path,)


def test_a_module_beside_a_plugin_is_not_global(tmp_path: Path) -> None:
    result = _select(_repo(tmp_path), "cli/plugins/other.py")

    assert (result.decision, result.tests) == ("SELECTED", _TOP_LEVEL)


def test_a_malformed_plugin_list_fails_fast(tmp_path: Path) -> None:
    repo_root = _repo(tmp_path)
    _write(repo_root, "conftest.py", 'pytest_plugins = "tests.fixtures.env"\n')

    with pytest.raises(TypeError, match="pytest_plugins"):
        _select(repo_root, "base/lm/engine.py")


def test_deleted_paths_are_ignored(tmp_path: Path) -> None:
    repo_root = _repo(tmp_path)
    alone = _select(repo_root, "base/lm/engine.py")

    mixed = _select(
        repo_root, "base/lm/removed.py", "tests/fixtures/removed.py", "base/lm/engine.py"
    )
    only_deleted = _select(repo_root, "base/lm/removed.py")

    assert (mixed.decision, mixed.tests) == (alone.decision, alone.tests)
    assert (only_deleted.decision, only_deleted.tests) == ("SELECTED", (_LINT,))


def test_a_frontend_file_adds_no_backend_test(tmp_path: Path) -> None:
    repo_root = _repo(tmp_path)
    alone = _select(repo_root, "base/lm/engine.py")

    frontend = _select(repo_root, "ui/web/src/App.tsx")
    mixed = _select(repo_root, "ui/web/src/App.tsx", "base/lm/engine.py")

    assert (frontend.decision, frontend.tests) == ("SELECTED", (_LINT,))
    assert mixed.tests == alone.tests


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".agents/skills/demo/SKILL.md",
        ".pre-commit-config.yaml",
        ".test_durations",
        ".test_durations.source.json",
        "LICENSE",
        "tests/e2e/test_browser.py",
        "tests/e2e/conftest.py",
        "tests/e2e/helper.py",
    ],
)
def test_repository_level_inputs_run_only_the_tree_scan_tests(tmp_path: Path, path: str) -> None:
    """The e2e jobs run the whole e2e package for any non-docs diff, so the
    backend subset needs only the tree-wide gates for these."""
    result = _select(_repo(tmp_path), path)

    assert (result.decision, result.tests) == ("SELECTED", (_LINT,))


def test_an_unowned_path_is_the_full_suite_safety_net(tmp_path: Path) -> None:
    repo_root = _repo(tmp_path)
    _write(repo_root, "mystery/thing.py")

    result = _select(repo_root, "mystery/thing.py", "base/lm/engine.py")

    assert (result.decision, result.reason) == ("FULL", "unmapped")
    assert result.blind_changed == ("mystery/thing.py",)


def _tracked_paths() -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    return [path for path in completed.stdout.split("\0") if path]


def test_every_tracked_path_has_an_owner() -> None:
    """The structural guarantee: no tracked path is unmapped on the trunk.

    A new top-level directory or root file must be given a rule in
    scripts/ci/test_selector.py; the runtime safety net (a full suite for an
    unmapped path) is then never taken for a path that already exists.
    """
    checkout = test_selector.load_checkout(_REPO_ROOT)

    unmapped = [
        path
        for path in _tracked_paths()
        if test_selector.classify_path(path, checkout) is PathClass.UNMAPPED
    ]

    assert unmapped == []


def test_the_real_root_conftest_plugins_are_global() -> None:
    checkout = test_selector.load_checkout(_REPO_ROOT)

    for path in (
        "conftest.py",
        "tests/fixtures/env_bootstrap.py",
        "cli/commands/tests/health_port_guard.py",
        "tests/_asyncio_stall_probe.py",
    ):
        assert test_selector.classify_path(path, checkout) is PathClass.GLOBAL, path
