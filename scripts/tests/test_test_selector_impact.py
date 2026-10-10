"""Runtime impact regressions independent of nearest-package ownership buckets."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.ci import test_selector


def _write(root: Path, name: str, text: str = "") -> None:
    file = root / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)


def _repo(root: Path) -> Path:
    _write(
        root,
        "pyproject.toml",
        '[tool.pytest.ini_options]\ntestpaths = ["tests", "base/**/tests"]\n',
    )
    _write(root, "base/unrelated/tests/test_filler.py", "def test_filler(): pass\n")
    _write(
        root,
        ".test_durations",
        json.dumps({"base/unrelated/tests/test_filler.py::test_filler": 1000}),
    )
    return root


def _assert_selected(
    root: Path, changed: str, expected: str, *, base_ref: str | None = None
) -> None:
    result = test_selector.select_tests([changed], repo_root=root, base_ref=base_ref)
    assert result.decision == "SELECTED", result.as_json()
    assert expected in result.tests


def test_transitive_relative_helpers_and_imported_tests_are_runtime_edges(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, "tests/support/inner.py", "from base import leaf\n")
    _write(root, "tests/support/outer.py", "from . import inner\n")
    _write(root, "base/tests/test_shared.py", "from tests.support import outer\n")
    _write(root, "tests/consumer/test_scenario.py", "from base.tests import test_shared\n")
    reverse = test_selector.build_import_reverse_map(root)
    assert reverse["base/leaf.py"] == {
        "base/tests/test_shared.py",
        "tests/consumer/test_scenario.py",
    }
    _assert_selected(root, "tests/support/inner.py", "tests/consumer/test_scenario.py")
    _assert_selected(root, "base/tests/test_shared.py", "tests/consumer/test_scenario.py")


def test_path_scoped_fixture_closure_reaches_tests_outside_its_owner(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, "tests/path_scoped/agent_tests.py", "from base import leaf\n")
    _write(root, "base/consumer/tests/test_scope.py", "def test_scope(): pass\n")
    _write(
        root, "base/consumer/tests/path_scopes.toml", '"tests.path_scoped.agent_tests" = ["."]\n'
    )
    _assert_selected(root, "tests/path_scoped/agent_tests.py", "base/consumer/tests/test_scope.py")
    _assert_selected(root, "base/leaf.py", "base/consumer/tests/test_scope.py")


def test_collecting_a_packaged_test_executes_its_package_initializers(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, "base/consumer/__init__.py", "from base import leaf\n")
    _write(root, "base/consumer/tests/test_scope.py", "def test_scope(): pass\n")
    _assert_selected(root, "base/leaf.py", "base/consumer/tests/test_scope.py")


def test_repository_directory_reads_reach_changed_descendants(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(
        root,
        "tests/consumer/test_scan.py",
        """from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
def test_scan():
    assert list(ROOT.rglob("*.py"))
""",
    )
    _assert_selected(root, "base/leaf.py", "tests/consumer/test_scan.py")


def test_global_plugins_and_local_conftests_are_real_runtime_dependencies(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, "tests/plugin.py", "from base import leaf\n")
    _write(root, "conftest.py", 'pytest_plugins = ["tests.plugin"]\n')
    result = test_selector.select_tests(["base/leaf.py"], repo_root=root)
    assert (result.decision, result.reason) == ("FULL", "subset-too-close")
    assert result.est_seconds == result.full_est_seconds
    (root / "conftest.py").unlink()
    _write(root, "base/consumer/tests/conftest.py", "from base import leaf\n")
    _write(root, "base/consumer/tests/test_scope.py", "def test_scope(): pass\n")
    _assert_selected(root, "base/leaf.py", "base/consumer/tests/test_scope.py")


@pytest.mark.parametrize(
    ("source", "changed"),
    [
        (
            'import subprocess\nsubprocess.run(["python", "-m", "services.agent_runner.pty_sessions.daemon"])\n',
            "services/agent_runner/pty_sessions/daemon.py",
        ),
        (
            'import subprocess\nsubprocess.run(["python", "-c", "from base import leaf"])\n',
            "base/leaf.py",
        ),
        (
            'from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\n(ROOT / "base/data/prompt.txt").read_text()\n',
            "base/data/prompt.txt",
        ),
    ],
)
def test_literal_process_and_resource_boundaries_select_the_consumer(
    tmp_path: Path,
    source: str,
    changed: str,
) -> None:
    root = _repo(tmp_path)
    _write(root, changed)
    _write(root, "tests/security/test_argv.py", source)
    _assert_selected(root, changed, "tests/security/test_argv.py")


def test_finite_parametrized_plugin_imports_select_the_registry_consumer(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "ava_builtins/plugins/ava_memory/metrics.py")
    _write(
        root,
        "tests/registry/test_metrics.py",
        """import importlib
import pytest
@pytest.mark.parametrize("plugin", ["ava_memory"])
def test_metrics(plugin):
    module_name = f"ava_builtins.plugins.{plugin}.metrics"
    importlib.import_module(module_name)
""",
    )
    _assert_selected(
        root, "ava_builtins/plugins/ava_memory/metrics.py", "tests/registry/test_metrics.py"
    )


def test_opaque_import_is_visible_full_and_syntax_errors_fail(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(
        root,
        "tests/consumer/test_dynamic.py",
        "import importlib\nimportlib.import_module(module_name)\n",
    )
    result = test_selector.select_tests(["base/leaf.py"], repo_root=root)
    assert (result.decision, result.reason) == ("FULL", "incomplete-impact")
    assert any("tests/consumer/test_dynamic.py:2:" in item for item in result.diagnostics)
    _write(root, "tests/consumer/test_dynamic.py", "def syntax error\n")
    with pytest.raises(SyntaxError):
        test_selector.select_tests(["base/leaf.py"], repo_root=root)


def test_unknown_in_an_unloaded_helper_does_not_force_full(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, "tests/support/orphan.py", "import importlib\nimportlib.import_module(name)\n")
    _write(root, "tests/consumer/test_leaf.py", "from base import leaf\n")
    _assert_selected(root, "base/leaf.py", "tests/consumer/test_leaf.py")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed Git commands against a test-owned checkout
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.parametrize("deleted", [False, True])
def test_base_facts_preserve_removed_dependency_impact(tmp_path: Path, deleted: bool) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, "tests/support/helper.py", "from base import leaf\n")
    _write(root, "tests/consumer/test_old.py", "from tests.support import helper\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    _write(root, "tests/support/helper.py")
    if deleted:
        (root / "base/leaf.py").unlink()
    _assert_selected(root, "base/leaf.py", "tests/consumer/test_old.py", base_ref=base)


def test_base_archive_cannot_silently_omit_dependency_inputs(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, ".gitattributes", "base/leaf.py export-ignore\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    with pytest.raises(RuntimeError, match=r"Base archive omitted tracked inputs.*base/leaf\.py"):
        test_selector.select_tests(["base/leaf.py"], repo_root=root, base_ref="HEAD")
