"""Every lint and hook that decides something about test files sees a package's own tests.

Tests live either in the top-level `tests/` or beside the code they prove, in
`<pkg>/**/tests/`. A tool whose scope is written as "the `tests/` directory" stops
covering a test the moment it moves into a package, and nothing reports it: the
lint still exits 0. A tool that exempts tests by directory the other way round
starts flagging them. Each test below pins one tool's behavior at the new location
against its behavior at the top-level `tests/`.
"""

from __future__ import annotations

import importlib
import re
import textwrap
from pathlib import Path

import pytest
import yaml

from scripts.structure import lint_common

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRE_COMMIT = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
_HOOKS = {hook["id"]: hook for repo in _PRE_COMMIT["repos"] for hook in repo["hooks"]}


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# ── the shared predicate ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("tests/agent/test_x.py", True),
        ("base/packages/tests/test_x.py", True),
        ("ava_builtins/skills/integrations/gmail/scripts/tests/test_gmail.py", True),
        ("base/db/test_db_guard.py", False),
        ("scripts/ci/test_selector.py", False),
        ("base/packages/attests/x.py", False),
    ],
)
def test_is_test_path(rel: str, expected: bool) -> None:
    assert lint_common.is_test_path(rel) is expected


def test_a_path_outside_the_repo_is_never_a_test(tmp_path: Path) -> None:
    outside = tmp_path / "tests" / "test_x.py"
    assert lint_common.is_repo_test_file(outside, tmp_path / "repo") is False
    assert lint_common.is_repo_test_file(tmp_path / "repo/base/tests/x.py", tmp_path / "repo")


# ── lints that exempt tests: the package location is exempt like tests/ ─────


def test_async_lint_exempts_a_package_tests_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lint = importlib.import_module("scripts.lint.async_no_sync_blocking")
    monkeypatch.setattr(lint, "_ROOT", tmp_path)
    monkeypatch.setattr(lint, "_DEFINITION_DIRS", ("base",))
    monkeypatch.setattr(lint, "_REPO_BLOCKING_HELPERS", set())
    monkeypatch.setattr(lint, "_BLOCKING_NAMES", lint._LIBRARY_BLOCKING_NAMES)
    body = "import time\n\nasync def handler() -> None:\n    time.sleep(1)\n"
    _write(tmp_path, "gateway/agents/tests/test_handler.py", body)
    _write(tmp_path, "ops/tests/test_ops.py", body)
    (tmp_path / "base").mkdir()
    assert lint.main() == 0

    _write(tmp_path, "gateway/agents/handler.py", body)
    assert lint.main() == 1


def test_async_lint_helper_names_are_not_kept_alive_by_a_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test that defines its own `sync_op` must not keep a retired helper's entry alive."""
    lint = importlib.import_module("scripts.lint.async_no_sync_blocking")
    monkeypatch.setattr(lint, "_ROOT", tmp_path)
    monkeypatch.setattr(lint, "_DEFINITION_DIRS", ("base",))
    monkeypatch.setattr(lint, "_REPO_BLOCKING_HELPERS", {"sync_op"})
    _write(tmp_path, "base/pkg/tests/test_x.py", "def sync_op() -> None:\n    pass\n")
    assert lint._stale_repo_helpers() == ["sync_op"]
    _write(tmp_path, "base/pkg/ops.py", "def sync_op() -> None:\n    pass\n")
    assert lint._stale_repo_helpers() == []


def test_plugin_wrap_lint_exempts_a_plugins_own_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lint = importlib.import_module("scripts.lint.plugins.no_plugin_wrap")
    monkeypatch.setattr(lint, "_REPO_ROOT", tmp_path)
    body = "import ava\n\nava.files.read = lambda: None\n"
    stub = _write(tmp_path, "ava_builtins/plugins/p/tests/test_plugin.py", body)
    assert lint.main([]) == 0
    assert lint.main([str(stub)]) == 0

    _write(tmp_path, "ava_builtins/plugins/p/plugin.py", body)
    assert lint.main([]) == 1


def test_ava_root_scope_lint_exempts_the_supervisors_own_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lint = importlib.import_module("scripts.lint.ava_root_scope")
    monkeypatch.setattr(lint, "_REPO_ROOT", tmp_path)
    proof = _write(
        tmp_path, "services/supervision/ava_root/tests/test_edge.py", 'NAME = "systemd"\n'
    )
    assert lint.main([]) == 0
    assert lint.main([str(proof)]) == 0

    _write(tmp_path, "services/supervision/ava_root/daemon.py", 'NAME = "systemd"\n')
    assert lint.main([]) == 1


# ── lints that scan tests on purpose: the default scan reaches the package ──


def test_fixture_scope_lint_default_scan_reaches_a_package_tests_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lint = importlib.import_module("scripts.lint.fixture_scope")
    monkeypatch.setattr(lint, "_REPO_ROOT", tmp_path)
    fixture = """
        import os

        import pytest


        @pytest.fixture(scope="session")
        def leaky() -> None:
            os.environ["AVA_X"] = "1"
    """
    _write(tmp_path, "tests/e2e/conftest.py", fixture)
    assert lint.main([]) == 1
    top_level = capsys.readouterr().out

    (tmp_path / "tests/e2e/conftest.py").unlink()
    _write(tmp_path, "base/packages/tests/conftest.py", fixture)
    assert lint.main([]) == 1
    in_package = capsys.readouterr().out
    assert in_package == top_level.replace("tests/e2e/", "base/packages/tests/")


def test_fixture_scope_lint_package_scope_needs_an_init_in_a_package_tests_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lint = importlib.import_module("scripts.lint.fixture_scope")
    monkeypatch.setattr(lint, "_REPO_ROOT", tmp_path)
    _write(
        tmp_path,
        "services/example/tests/conftest.py",
        """
        import pytest


        @pytest.fixture(scope="package")
        def shared() -> None:
            return None
        """,
    )
    assert lint.main([]) == 1
    assert 'scope="package"' in capsys.readouterr().out


def test_fixture_scope_lint_ignores_a_module_that_is_not_a_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lint = importlib.import_module("scripts.lint.fixture_scope")
    monkeypatch.setattr(lint, "_REPO_ROOT", tmp_path)
    _write(
        tmp_path,
        "base/packages/fixtures.py",
        """
        import os

        import pytest


        @pytest.fixture(scope="session")
        def leaky() -> None:
            os.environ["AVA_X"] = "1"
        """,
    )
    assert lint.main([]) == 0


def _time_bomb_root(root: Path) -> None:
    for name in (*lint_common.FRAMEWORK_DIRS, "scripts", "tests"):
        (root / name).mkdir(exist_ok=True)


def test_time_bomb_lint_default_scan_reaches_a_package_tests_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rules 2 and 3 read test files: the default scan must find the ones inside packages."""
    lint = importlib.import_module("scripts.lint.diagnostics.time_bomb")
    monkeypatch.setattr(lint, "_REPO_ROOT", tmp_path)
    _time_bomb_root(tmp_path)
    _write(tmp_path, "tests/test_window.py", 'since = "2026-09-06"\n')
    assert lint.main([]) == 1
    assert "time-bomb fixture date" in capsys.readouterr().err

    (tmp_path / "tests/test_window.py").unlink()
    _write(tmp_path, "base/packages/tests/test_window.py", 'since = "2026-09-06"\n')
    assert lint.main([]) == 1
    assert "base/packages/tests/test_window.py" in capsys.readouterr().err


def test_time_bomb_lint_rule_3_does_not_govern_production_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lint = importlib.import_module("scripts.lint.diagnostics.time_bomb")
    monkeypatch.setattr(lint, "_REPO_ROOT", tmp_path)
    _time_bomb_root(tmp_path)
    _write(tmp_path, "base/packages/window.py", 'since = "2026-09-06"\n')
    assert lint.main([]) == 0


@pytest.mark.parametrize("host", ["cli", "ops", "ava_builtins/plugins/p"])
def test_no_os_environ_default_scan_reaches_tests_in_dirs_it_never_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """Rule 2 (setenv on a Settings-managed alias) read `tests/cli` and `tests/ops`; the
    same tests inside `cli/`, `ops/` and `ava_builtins/` must stay read."""
    lint = importlib.import_module("scripts.lint.no_os_environ")
    monkeypatch.setattr(lint, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint, "_settings_managed_aliases", lambda: frozenset({"AVA_DB_URL"}))
    body = """
        def test_x(monkeypatch) -> None:
            monkeypatch.setenv("AVA_DB_URL", "x")
    """
    for name in (*lint_common.FRAMEWORK_DIRS, "tests"):
        (tmp_path / name).mkdir(exist_ok=True)
    _write(tmp_path, f"{host}/tests/test_x.py", body)
    assert lint.main([]) == 1


def test_no_os_environ_default_scan_still_reads_only_tests_in_those_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extra roots are read for test files only: Rule 1 does not start covering them."""
    lint = importlib.import_module("scripts.lint.no_os_environ")
    monkeypatch.setattr(lint, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint, "_settings_managed_aliases", lambda: frozenset({"AVA_DB_URL"}))
    for name in (*lint_common.FRAMEWORK_DIRS, "tests"):
        (tmp_path / name).mkdir(exist_ok=True)
    _write(tmp_path, "cli/pkg/mod.py", 'import os\n\nHOME = os.environ["HOME"]\n')
    assert lint.main([]) == 0


# ── pre-commit hooks that select by directory prefix ────────────────────────


def _hook_selects(hook_id: str, path: str) -> bool:
    return re.search(_HOOKS[hook_id]["files"], path) is not None


@pytest.mark.parametrize(
    ("hook_id", "path", "selected"),
    [
        ("lint-fixture-scope", "tests/e2e/conftest.py", True),
        ("lint-fixture-scope", "base/packages/tests/conftest.py", True),
        (
            "lint-fixture-scope",
            "ava_builtins/skills/integrations/gmail/scripts/tests/conftest.py",
            True,
        ),
        ("lint-fixture-scope", "base/packages/conftest_helper.py", False),
        # The async lint and the ava-root lint skip test files, so a test edit does not run them.
        ("lint-async-no-sync-blocking", "gateway/routers/agents.py", True),
        ("lint-async-no-sync-blocking", "gateway/agents/tests/test_x.py", False),
        ("lint-async-no-sync-blocking", "ops/tests/test_x.py", False),
        ("lint-ava-root-scope", "services/supervision/ava_root/daemon.py", True),
        ("lint-ava-root-scope", "services/supervision/ava_root/tests/test_daemon.py", False),
        # A generated-artifact freshness check is triggered by its sources, never by their tests.
        ("types-codegen-fresh", "gateway/routers/agents.py", True),
        ("types-codegen-fresh", "gateway/agents/tests/test_x.py", False),
        ("types-codegen-fresh", "base/api_contracts/contracts.py", True),
        ("types-codegen-fresh", "base/api_contracts/tests/test_contracts.py", False),
        ("types-codegen-fresh", "ops/rpc_schemas/tests/test_terminate.py", False),
        ("config-lite-table-fresh", "base/config/base.py", True),
        ("config-lite-table-fresh", "base/config/tests/test_config.py", False),
        # The generated pyright tests environments follow the tests directories themselves:
        # any module in a package's tests/ directory can add or remove one.
        ("lint-pyright-test-environments", "base/packages/tests/test_x.py", True),
        (
            "lint-pyright-test-environments",
            "ava_builtins/skills/integrations/gmail/scripts/tests/x.py",
            True,
        ),
        ("lint-pyright-test-environments", "pyproject.toml", True),
        ("lint-pyright-test-environments", "tests/agent/test_x.py", False),
        ("lint-pyright-test-environments", "base/packages/plugins.py", False),
    ],
)
def test_pre_commit_hook_selection_by_directory(hook_id: str, path: str, selected: bool) -> None:
    assert _hook_selects(hook_id, path) is selected
