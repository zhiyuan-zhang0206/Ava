"""Exec pre-spawn behavior when the interpreter's editable install is poisoned."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from agent.graph._exec_result import ExecChildError, _ExecCrashed, _ExecDone
from agent.graph._exec_subprocess import _run_in_subprocess

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


async def test_healthy_editable_guard_preserves_real_child_behavior(tmp_path: Path) -> None:
    result = await _run(tmp_path, editable_guard=lambda: ())

    assert isinstance(result, _ExecDone)
    assert result.output == "healthy child\n"
