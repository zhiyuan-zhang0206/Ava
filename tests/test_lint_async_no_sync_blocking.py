"""`scripts/lint_async_no_sync_blocking.py` — sync calls in async bodies, and a
current repo-helper list (a retired helper's name must not linger)."""

from __future__ import annotations

import importlib
import textwrap
from pathlib import Path

import pytest

_lint = importlib.import_module("scripts.lint_async_no_sync_blocking")


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch source tree with one repo helper defined in `shared/`."""
    monkeypatch.setattr(_lint, "_ROOT", tmp_path)
    monkeypatch.setattr(_lint, "_DEFINITION_DIRS", ("shared",))
    monkeypatch.setattr(_lint, "_SCAN_DIRS", ("gateway",))
    monkeypatch.setattr(_lint, "_REPO_BLOCKING_HELPERS", {"sync_op"})
    monkeypatch.setattr(_lint, "_BLOCKING_NAMES", _lint._LIBRARY_BLOCKING_NAMES | {"sync_op"})
    _write(tmp_path, "shared/ops.py", "def sync_op() -> None:\n    pass\n")
    (tmp_path / "gateway").mkdir()
    return tmp_path


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def test_real_repo_helpers_are_all_defined() -> None:
    assert _lint._stale_repo_helpers() == []


def test_a_retired_helper_name_is_stale(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_lint, "_REPO_BLOCKING_HELPERS", {"sync_op", "cluster_rollout_op"})
    assert _lint._stale_repo_helpers() == ["cluster_rollout_op"]


def test_a_stale_helper_fails_the_lint(
    tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_lint, "_REPO_BLOCKING_HELPERS", {"sync_op", "cluster_update_op"})
    assert _lint.main() == 1
    assert "stale _REPO_BLOCKING_HELPERS entry 'cluster_update_op'" in capsys.readouterr().out


def test_async_helper_definitions_count(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tree, "shared/aio.py", "async def async_op[T](value: T) -> T:\n    return value\n")
    monkeypatch.setattr(_lint, "_REPO_BLOCKING_HELPERS", {"sync_op", "async_op"})
    assert _lint._stale_repo_helpers() == []


def test_unthreaded_repo_helper_in_async_body_is_flagged(tree: Path) -> None:
    _write(
        tree,
        "gateway/route.py",
        """
        async def handler() -> None:
            sync_op()
        """,
    )
    assert _lint.main() == 1


def test_threaded_repo_helper_is_clean(tree: Path) -> None:
    _write(
        tree,
        "gateway/route.py",
        """
        import asyncio

        async def handler() -> None:
            await asyncio.to_thread(sync_op)
        """,
    )
    assert _lint.main() == 0
