"""Exercise process ownership through real pytest collection and execution."""

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def owned_tree(tmp_path: Path) -> Path:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(
        "from tests.fixtures import static_environment as environment\n"
        "environment.STATIC_TEST_PATHS = ('tools/tests', 'canaries/test_boundary.py')\n"
        "pytest_plugins = ['tests.fixtures.env_bootstrap', "
        "'tests.fixtures.static_environment']\n",
        encoding="utf-8",
    )
    for rel in (
        "tools/tests/test_tool.py",
        "tools/tests/nested/test_child.py",
        "canaries/test_boundary.py",
        "native/test_database.py",
        "tools/tests_backup/test_neighbor.py",
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        assertion = (
            "os.environ['STATIC_SCOPE_VALUE'] == 'retained'"
            if rel.startswith("tools/tests/")
            else "2 + 2 == 4"
        )
        path.write_text(
            f"import os\ndef test_contract():\n    assert {assertion}\n", encoding="utf-8"
        )
    (tmp_path / "tools/tests/conftest.py").write_text(
        "import pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def retained_scope(monkeypatch):\n"
        "    monkeypatch.setenv('STATIC_SCOPE_VALUE', 'retained')\n",
        encoding="utf-8",
    )
    return tmp_path


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return subprocess.run(  # noqa: S603 — own interpreter, synthetic test-owned tree
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _collected(root: Path, *args: str) -> set[str]:
    result = _run(root, "--collect-only", *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return {line for line in result.stdout.splitlines() if "::test_contract" in line}


def test_static_and_native_ownership_partition_collection_and_keep_local_fixtures(
    owned_tree: Path,
) -> None:
    whole = _collected(owned_tree)
    static = _collected(owned_tree, "--test-environment=static")
    native = _collected(owned_tree, "--omit-static-tests")
    assert len(whole) == 5
    assert static == {
        "tools/tests/test_tool.py::test_contract",
        "tools/tests/nested/test_child.py::test_contract",
        "canaries/test_boundary.py::test_contract",
    }
    assert static.isdisjoint(native)
    assert whole == static | native
    result = _run(owned_tree, "--test-environment=static")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout


def test_static_selection_accepts_directory_descendants_and_node_ids(owned_tree: Path) -> None:
    selected = _collected(owned_tree, "--test-environment=static", "tools/tests/nested")
    assert selected == {"tools/tests/nested/test_child.py::test_contract"}
    assert _collected(owned_tree, "--test-environment=static", *selected) == selected
    assert _collected(
        owned_tree, "--omit-static-tests", "tools/tests", "native/test_database.py"
    ) == {"native/test_database.py::test_contract"}


@pytest.mark.parametrize("path", ["native", "tools", "tools/tests_backup", "tools/tests/escape"])
def test_static_selection_refuses_unowned_and_escaping_paths(owned_tree: Path, path: str) -> None:
    (owned_tree / "tools/tests/escape").symlink_to(owned_tree / "native", target_is_directory=True)
    result = _run(owned_tree, "--test-environment=static", path)
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "Static processes accept only owned static test paths" in result.stderr


def test_missing_ownership_and_conflicting_modes_fail_before_execution(owned_tree: Path) -> None:
    conflict = _run(owned_tree, "--test-environment=static", "--omit-static-tests")
    assert conflict.returncode == pytest.ExitCode.USAGE_ERROR, conflict.stdout + conflict.stderr
    assert "cannot omit its own tests" in conflict.stderr
    (owned_tree / "canaries/test_boundary.py").unlink()
    missing = _run(owned_tree, "--test-environment=static")
    assert missing.returncode == pytest.ExitCode.USAGE_ERROR, missing.stdout + missing.stderr
    assert "Static test ownership names missing paths" in missing.stderr


def test_static_collection_refuses_a_descendant_symlink_to_native_tests(owned_tree: Path) -> None:
    (owned_tree / "tools/tests/test_escape.py").symlink_to(owned_tree / "native/test_database.py")
    result = _run(owned_tree, "--test-environment=static", "--collect-only")
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "Non-static tests reached the static process" in result.stderr


def test_actual_ci_warning_policy_rejects_background_native_driver_calls(owned_tree: Path) -> None:
    (owned_tree / "tools/tests/test_tool.py").write_text(
        "import threading\nimport psycopg\n"
        "def test_forbidden_background_call():\n"
        "    thread = threading.Thread(target=psycopg.connect, "
        "args=('postgresql://unused@127.0.0.1:1/unused',))\n"
        "    thread.start()\n    thread.join()\n",
        encoding="utf-8",
    )
    workflow = Path(__file__).resolve().parents[3] / ".github/workflows/ci.yml"
    jobs = yaml.safe_load(workflow.read_text())["jobs"]
    command = next(
        step["run"]
        for job in jobs.values()
        for step in job["steps"]
        if step.get("name") == "Run static pytest contracts"
    )
    arguments = shlex.split(command)
    warning_args: list[str] = []
    for index, argument in enumerate(arguments):
        if argument == "-W":
            warning_args.extend((argument, arguments[index + 1]))
        elif argument.startswith("-W"):
            warning_args.append(argument)
    result = _run(
        owned_tree, "--test-environment=static", "tools/tests/test_tool.py", *warning_args
    )
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout + result.stderr
    assert "PytestUnhandledThreadExceptionWarning" in result.stdout + result.stderr
    assert "Static tests cannot use Postgres or Redis" in result.stdout + result.stderr
