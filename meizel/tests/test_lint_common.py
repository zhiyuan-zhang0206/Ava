from __future__ import annotations

from pathlib import Path

import pytest

from meizel import lint_common

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_framework_dir_exists() -> None:
    roots = lint_common.scan_roots(_REPO_ROOT, lint_common.FRAMEWORK_DIRS)
    assert [r.name for r in roots] == list(lint_common.FRAMEWORK_DIRS)


def test_missing_scan_dir_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "agent").mkdir()
    with pytest.raises(FileNotFoundError, match="plugins, mcps"):
        lint_common.scan_roots(tmp_path, ("agent", "plugins", "mcps"))


@pytest.mark.parametrize(
    "pattern", ["/**/tests", "*/**/tests", "base/**/test", "base/../tests", "base\\child/**/tests"]
)
def test_pytest_test_scope_rejects_unsupported_directory_patterns(pattern: str) -> None:
    with pytest.raises(ValueError, match="unsupported pytest testpaths"):
        lint_common.pytest_test_hosts(f"[tool.pytest.ini_options]\ntestpaths = [{pattern!r}]\n")


def test_pytest_test_scope_rejects_duplicate_hosts() -> None:
    with pytest.raises(ValueError, match="duplicate pytest test host"):
        lint_common.pytest_test_hosts('[tool.pytest.ini_options]\ntestpaths = ["tests", "tests"]\n')
