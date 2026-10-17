from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest
import yaml

from meizel import lint_common

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRE_COMMIT = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
_HOOKS = {hook["id"]: hook for repo in _PRE_COMMIT["repos"] for hook in repo["hooks"]}


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("tests/components/agent/test_x.py", True),
        ("base/packages/tests/test_x.py", True),
        ("meiva_builtins/skills/integrations/gmail/scripts/tests/test_gmail.py", True),
        ("base/db/test_db_guard.py", False),
        ("scripts/ci/test_selector.py", False),
        ("scripts/ci/test_impact.py", False),
        ("base/packages/attests/x.py", False),
    ],
)
def test_is_test_path(rel: str, expected: bool) -> None:
    assert lint_common.is_test_path(rel) is expected


def test_a_path_outside_the_repo_is_never_a_test(tmp_path: Path) -> None:
    outside = tmp_path / "tests" / "test_x.py"
    assert lint_common.is_repo_test_file(outside, tmp_path / "repo") is False
    assert lint_common.is_repo_test_file(tmp_path / "repo/base/tests/x.py", tmp_path / "repo")


def test_async_lint_exempts_a_package_tests_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.lint import async_no_sync_blocking as lint

    monkeypatch.setattr(lint, "_ROOT", tmp_path)
    monkeypatch.setattr(lint, "DEFINITION_DIRS", ("base",))
    monkeypatch.setattr(lint, "REPO_BLOCKING_HELPERS", set[str]())
    monkeypatch.setattr(lint, "BLOCKING_NAMES", lint.LIBRARY_BLOCKING_NAMES)
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
    from scripts.lint import async_no_sync_blocking as lint

    monkeypatch.setattr(lint, "_ROOT", tmp_path)
    monkeypatch.setattr(lint, "DEFINITION_DIRS", ("base",))
    monkeypatch.setattr(lint, "REPO_BLOCKING_HELPERS", {"sync_op"})
    _write(tmp_path, "base/pkg/tests/test_x.py", "def sync_op() -> None:\n    pass\n")
    assert lint.stale_repo_helpers() == ["sync_op"]
    _write(tmp_path, "base/pkg/ops.py", "def sync_op() -> None:\n    pass\n")
    assert lint.stale_repo_helpers() == []


def test_plugin_wrap_lint_exempts_a_plugins_own_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.lint.plugins import no_plugin_wrap as lint

    monkeypatch.setattr(lint, "_REPO_ROOT", tmp_path)
    body = "import meiva\n\nmeiva.files.read = lambda: None\n"
    stub = _write(tmp_path, "meiva_builtins/plugins/p/tests/test_plugin.py", body)
    assert lint.main([]) == 0
    assert lint.main([str(stub)]) == 0

    _write(tmp_path, "meiva_builtins/plugins/p/plugin.py", body)
    assert lint.main([]) == 1


def test_meiva_root_scope_lint_exempts_the_supervisors_own_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.lint import meiva_root_scope as lint

    monkeypatch.setattr(lint, "_REPO_ROOT", tmp_path)
    proof = _write(
        tmp_path, "services/supervision/meiva_root/tests/test_edge.py", 'NAME = "systemd"\n'
    )
    assert lint.main([]) == 0
    assert lint.main([str(proof)]) == 0

    _write(tmp_path, "services/supervision/meiva_root/daemon.py", 'NAME = "systemd"\n')
    assert lint.main([]) == 1


def test_fixture_scope_lint_default_scan_reaches_a_package_tests_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.lint import fixture_scope as lint

    monkeypatch.setattr(lint, "_REPO_ROOT", tmp_path)
    _write(
        tmp_path,
        "pyproject.toml",
        '[tool.pytest.ini_options]\ntestpaths = ["tests", "base/**/tests", "services/**/tests"]\n',
    )
    fixture = """
        import os

        import pytest


        @pytest.fixture(scope="session")
        def leaky() -> None:
            os.environ["MEIVA_X"] = "1"
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
    from scripts.lint import fixture_scope as lint

    monkeypatch.setattr(lint, "_REPO_ROOT", tmp_path)
    _write(
        tmp_path,
        "pyproject.toml",
        '[tool.pytest.ini_options]\ntestpaths = ["tests", "base/**/tests", "services/**/tests"]\n',
    )
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
    from scripts.lint import fixture_scope as lint

    monkeypatch.setattr(lint, "_REPO_ROOT", tmp_path)
    _write(
        tmp_path,
        "pyproject.toml",
        '[tool.pytest.ini_options]\ntestpaths = ["tests", "base/**/tests", "services/**/tests"]\n',
    )
    _write(
        tmp_path,
        "base/packages/fixtures.py",
        """
        import os

        import pytest


        @pytest.fixture(scope="session")
        def leaky() -> None:
            os.environ["MEIVA_X"] = "1"
        """,
    )
    assert lint.main([]) == 0


def _time_bomb_root(root: Path) -> None:
    for name in (*lint_common.FRAMEWORK_DIRS, "scripts", "meizel", "tests"):
        (root / name).mkdir(exist_ok=True)


def test_time_bomb_lint_default_scan_reaches_a_package_tests_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.lint.diagnostics import time_bomb as lint

    _time_bomb_root(tmp_path)
    _write(tmp_path, "tests/test_window.py", 'since = "2026-09-06"\n')
    assert lint.main([], repo_root=tmp_path) == 1
    assert "time-bomb fixture date" in capsys.readouterr().err

    (tmp_path / "tests/test_window.py").unlink()
    _write(tmp_path, "base/packages/tests/test_window.py", 'since = "2026-09-06"\n')
    assert lint.main([], repo_root=tmp_path) == 1
    assert "base/packages/tests/test_window.py" in capsys.readouterr().err


def test_time_bomb_lint_rule_3_does_not_govern_production_modules(tmp_path: Path) -> None:
    from scripts.lint.diagnostics import time_bomb as lint

    _time_bomb_root(tmp_path)
    _write(tmp_path, "base/packages/window.py", 'since = "2026-09-06"\n')
    assert lint.main([], repo_root=tmp_path) == 0


@pytest.mark.parametrize("host", ["cli", "ops", "meiva_builtins/plugins/p"])
def test_no_os_environ_default_scan_reaches_tests_in_dirs_it_never_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    from scripts.lint import no_os_environ as lint

    monkeypatch.setattr(lint, "settings_managed_aliases", lambda: frozenset({"MEIVA_DB_URL"}))
    body = """
        def test_x(monkeypatch) -> None:
            monkeypatch.setenv("MEIVA_DB_URL", "x")
    """
    for name in (*lint_common.FRAMEWORK_DIRS, "tests"):
        (tmp_path / name).mkdir(exist_ok=True)
    _write(tmp_path, f"{host}/tests/test_x.py", body)
    assert lint.main([], repo_root=tmp_path) == 1


def test_no_os_environ_default_scan_still_reads_only_tests_in_those_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.lint import no_os_environ as lint

    monkeypatch.setattr(lint, "settings_managed_aliases", lambda: frozenset({"MEIVA_DB_URL"}))
    for name in (*lint_common.FRAMEWORK_DIRS, "tests"):
        (tmp_path / name).mkdir(exist_ok=True)
    _write(tmp_path, "cli/pkg/mod.py", 'import os\n\nHOME = os.environ["HOME"]\n')
    assert lint.main([], repo_root=tmp_path) == 0


def _hook_selects(hook_id: str, path: str) -> bool:
    return re.search(_HOOKS[hook_id]["files"], path) is not None


@pytest.mark.parametrize(
    ("hook_id", "path", "selected"),
    [
        ("lint-fixture-scope", "tests/e2e/conftest.py", True),
        ("lint-fixture-scope", "pyproject.toml", True),
        ("lint-fixture-scope", "base/packages/tests/conftest.py", True),
        (
            "lint-fixture-scope",
            "meiva_builtins/skills/integrations/gmail/scripts/tests/conftest.py",
            True,
        ),
        ("lint-fixture-scope", "base/packages/conftest_helper.py", False),
        ("lint-async-no-sync-blocking", "gateway/routers/agents.py", True),
        ("lint-async-no-sync-blocking", "gateway/agents/tests/test_x.py", False),
        ("lint-async-no-sync-blocking", "ops/tests/test_x.py", False),
        ("lint-meiva-root-scope", "services/supervision/meiva_root/daemon.py", True),
        ("lint-meiva-root-scope", "services/supervision/meiva_root/tests/test_daemon.py", False),
        ("types-codegen-fresh", "gateway/routers/agents.py", True),
        ("types-codegen-fresh", "gateway/agents/tests/test_x.py", False),
        ("types-codegen-fresh", "base/api_contracts/contracts.py", True),
        ("types-codegen-fresh", "base/api_contracts/tests/test_contracts.py", False),
        ("types-codegen-fresh", "ops/rpc_schemas/tests/test_terminate.py", False),
        ("config-lite-table-fresh", "base/config/base.py", True),
        ("config-lite-table-fresh", "base/config/tests/test_config.py", False),
        ("lint-pyright-test-environments", "base/packages/tests/test_x.py", True),
        (
            "lint-pyright-test-environments",
            "meiva_builtins/skills/integrations/gmail/scripts/tests/x.py",
            True,
        ),
        ("lint-pyright-test-environments", "pyproject.toml", True),
        ("lint-pyright-test-environments", "tests/components/agent/test_x.py", False),
        ("lint-pyright-test-environments", "base/packages/plugins.py", False),
    ],
)
def test_pre_commit_hook_selection_by_directory(hook_id: str, path: str, selected: bool) -> None:
    assert _hook_selects(hook_id, path) is selected
