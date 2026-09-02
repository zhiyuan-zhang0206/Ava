"""Exec pre-spawn behavior when the interpreter's editable install is poisoned."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from agent.graph import _exec_subprocess
from agent.graph._exec_result import ExecChildError, _ExecCrashed, _ExecDone
from agent.graph._exec_subprocess import _run_in_subprocess
from shared import editable_install

_AGENT_ID = 424242


async def _run(
    tmp_path: Path,
    *,
    editable_guard: Callable[[], tuple[str, ...]],
) -> object:
    result, _payload = await _run_in_subprocess(
        "print('healthy child')",
        _AGENT_ID,
        asyncio.Event(),
        30.0,
        exec_dir=tmp_path / "exec",
        editable_guard=editable_guard,
    )
    return result


async def test_poisoned_editable_install_returns_retryable_crash_without_request_file(
    tmp_path: Path,
) -> None:
    poisoned_pth = (
        tmp_path
        / "prod"
        / ".venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "_editable_impl_ava.pth"
    )
    deleted_worktree = tmp_path / "deleted-worktree"
    violation = f"{poisoned_pth} names {str(deleted_worktree)!r}"

    result = await _run(tmp_path, editable_guard=lambda: (violation,))

    assert isinstance(result, _ExecCrashed)
    assert isinstance(result.exc, ExecChildError)
    assert result.exc.exc_type == "exec_editable_install_poisoned"
    assert violation in result.output
    assert "auto-repaired" in result.output
    assert "retry" in result.output.lower()
    assert not list((tmp_path / "exec").rglob("*.json"))


async def test_editable_guard_repair_failure_tells_agent_not_to_retry(tmp_path: Path) -> None:
    failure = PermissionError("site-packages is read-only")

    def raise_failure() -> tuple[str, ...]:
        raise failure

    result = await _run(tmp_path, editable_guard=raise_failure)

    assert isinstance(result, _ExecCrashed)
    assert result.exc is failure
    assert "site-packages is read-only" in result.output
    assert "do not retry" in result.output.lower()
    assert not list((tmp_path / "exec").rglob("*.json"))


async def test_editable_guard_unresolved_records_tell_agent_to_use_operator_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unresolved repair must not promise an execute-code retry will work."""

    source_root = tmp_path / "source"

    def remaining_violations(
        _root: Path,
        *,
        allowed_roots: tuple[Path, ...] = (),
    ) -> tuple[str, ...]:
        return ("pointer remains unreadable",)

    monkeypatch.setattr(editable_install, "current_interpreter_source_root", lambda: source_root)
    monkeypatch.setattr(editable_install, "editable_install_violations", remaining_violations)

    result = await _run(tmp_path, editable_guard=lambda: ("pointer was poisoned",))

    assert isinstance(result, _ExecCrashed)
    assert "operator recovery" in result.output
    assert "ava converge" in result.output
    assert "do not retry" in result.output.lower()


async def test_healthy_editable_guard_preserves_real_child_behavior(tmp_path: Path) -> None:
    result = await _run(tmp_path, editable_guard=lambda: ())

    assert isinstance(result, _ExecDone)
    assert result.output == "healthy child\n"


def test_child_env_drops_foreign_virtual_env_but_preserves_its_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exec children cannot use a checkout venv when inherited cwd is elsewhere."""

    source_root = tmp_path / "source"
    inside = source_root / "agent"
    sibling_worktree = source_root / ".worktrees" / "feature"
    claude_sibling_worktree = source_root / ".claude" / "worktrees" / "feature"
    outside = tmp_path / "worktree"
    inside.mkdir(parents=True)
    sibling_worktree.mkdir(parents=True)
    claude_sibling_worktree.mkdir(parents=True)
    outside.mkdir()
    monkeypatch.setattr(os, "environ", {"VIRTUAL_ENV": "/source/.venv"})
    monkeypatch.setattr(editable_install, "current_interpreter_source_root", lambda: source_root)

    monkeypatch.chdir(outside)
    foreign_env = _exec_subprocess._build_child_env(None, tmp_path / "request", tmp_path / "result")

    monkeypatch.chdir(inside)
    own_env = _exec_subprocess._build_child_env(None, tmp_path / "request", tmp_path / "result")

    monkeypatch.chdir(sibling_worktree)
    sibling_env = _exec_subprocess._build_child_env(None, tmp_path / "request", tmp_path / "result")

    monkeypatch.chdir(claude_sibling_worktree)
    claude_sibling_env = _exec_subprocess._build_child_env(
        None,
        tmp_path / "request",
        tmp_path / "result",
    )

    monkeypatch.setattr(editable_install, "current_interpreter_source_root", lambda: None)
    no_root_env = _exec_subprocess._build_child_env(None, tmp_path / "request", tmp_path / "result")

    assert "VIRTUAL_ENV" not in foreign_env
    assert own_env["VIRTUAL_ENV"] == "/source/.venv"
    assert "VIRTUAL_ENV" not in sibling_env
    assert "VIRTUAL_ENV" not in claude_sibling_env
    assert no_root_env["VIRTUAL_ENV"] == "/source/.venv"
