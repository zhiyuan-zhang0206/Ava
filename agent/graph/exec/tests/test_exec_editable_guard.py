"""Exec pre-spawn behavior when the interpreter's editable install is poisoned."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from agent.graph.exec import _subprocess
from agent.graph.exec._result import ExecChildError, _ExecCrashed, _ExecDone
from agent.graph.exec._subprocess import _run_in_subprocess
from base.db import Database
from base.deploy.release import editable_install
from tests.fixtures.pin_agent import exec_context

_AGENT_ID = 424242


async def _run(
    tmp_path: Path,
    *,
    editable_guard: Callable[[], tuple[str, ...]] | None,
) -> object:
    result, _payload = await _run_in_subprocess(
        Database.from_settings(),
        "print('healthy child')",
        exec_context(_AGENT_ID),
        asyncio.Event(),
        30.0,
        exec_dir=tmp_path / "exec",
        editable_guard=editable_guard,
        accumulation_max_chars=1_000_000,
    )
    return result


async def test_poisoned_editable_install_refuses_without_request_file(
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
    assert "no child was started" in result.output
    assert "do not retry" in result.output.lower()
    assert not list((tmp_path / "exec").rglob("*.json"))


async def test_editable_guard_inspection_failure_tells_agent_not_to_retry(tmp_path: Path) -> None:
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
    foreign_env = _subprocess._build_child_env(None, tmp_path / "request", tmp_path / "result")

    monkeypatch.chdir(inside)
    own_env = _subprocess._build_child_env(None, tmp_path / "request", tmp_path / "result")

    monkeypatch.chdir(sibling_worktree)
    sibling_env = _subprocess._build_child_env(None, tmp_path / "request", tmp_path / "result")

    monkeypatch.chdir(claude_sibling_worktree)
    claude_sibling_env = _subprocess._build_child_env(
        None,
        tmp_path / "request",
        tmp_path / "result",
    )

    monkeypatch.setattr(editable_install, "current_interpreter_source_root", lambda: None)
    no_root_env = _subprocess._build_child_env(None, tmp_path / "request", tmp_path / "result")

    assert "VIRTUAL_ENV" not in foreign_env
    assert own_env["VIRTUAL_ENV"] == "/source/.venv"
    assert "VIRTUAL_ENV" not in sibling_env
    assert "VIRTUAL_ENV" not in claude_sibling_env
    assert no_root_env["VIRTUAL_ENV"] == "/source/.venv"


async def test_default_guard_refuses_real_poison_without_repairing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    packages = source / ".venv" / "lib" / "python3.12" / "site-packages"
    packages.mkdir(parents=True)
    pointer = packages / editable_install.EDITABLE_PTH_NAME
    pointer.write_text(str(tmp_path / "removed-checkout"))
    metadata = packages / "ava-0.1.5.dist-info" / "direct_url.json"
    metadata.parent.mkdir()
    metadata.write_text(
        json.dumps(
            {"url": (tmp_path / "removed-checkout").as_uri(), "dir_info": {"editable": True}}
        )
    )
    before = {path: path.read_bytes() for path in (pointer, metadata)}
    monkeypatch.setattr(editable_install, "current_interpreter_source_root", lambda: source)

    result = await _run(tmp_path, editable_guard=None)

    assert isinstance(result, _ExecCrashed)
    assert "no child was started" in result.output
    assert {path: path.read_bytes() for path in before} == before
    assert not list((tmp_path / "exec").rglob("*.json"))
