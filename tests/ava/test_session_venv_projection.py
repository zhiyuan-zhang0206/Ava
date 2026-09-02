"""Session VIRTUAL_ENV projection follows the session working directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from ava.shell import sessions


class _CapturingBackend:
    """Minimal session backend that records the environment handed to a shell."""

    def __init__(self) -> None:
        self.environments: list[dict[str, str]] = []

    def new_session(
        self,
        _name: str,
        _command: str,
        _cwd: Path,
        *,
        env: dict[str, str],
    ) -> bool:
        self.environments.append(env)
        return True


def test_create_session_activates_only_checkout_cwds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Worktree sessions retain PATH but cannot inherit another checkout's venv."""

    checkout = tmp_path / "checkout"
    inside = checkout / "nested"
    sibling_worktree = checkout / ".worktrees" / "feature"
    claude_sibling_worktree = checkout / ".claude" / "worktrees" / "feature"
    outside = tmp_path / "worktree"
    inside.mkdir(parents=True)
    sibling_worktree.mkdir(parents=True)
    claude_sibling_worktree.mkdir(parents=True)
    outside.mkdir()
    backend = _CapturingBackend()
    activations: list[bool] = []
    monkeypatch.setattr(sessions, "_next_session_index_from_db", lambda: 1)
    monkeypatch.setattr(sessions, "_shell_prefix", lambda: "session-")
    monkeypatch.setattr(sessions, "get_shell_backend", lambda: backend)
    monkeypatch.setattr(sessions, "repo_root", lambda: checkout)

    def forward(*, activate_venv: bool = True) -> dict[str, str]:
        activations.append(activate_venv)
        return {"PATH": "/venv/bin:/usr/bin", **({"VIRTUAL_ENV": "/venv"} if activate_venv else {})}

    monkeypatch.setattr(sessions, "forward_env_dict", forward)

    sessions._create_session("inside", cwd=str(inside))
    sessions._create_session("sibling", cwd=str(sibling_worktree))
    sessions._create_session("claude-sibling", cwd=str(claude_sibling_worktree))
    sessions._create_session("outside", cwd=str(outside))

    assert activations == [True, False, False, False]
    assert backend.environments[0]["VIRTUAL_ENV"] == "/venv"
    assert "VIRTUAL_ENV" not in backend.environments[1]
    assert "VIRTUAL_ENV" not in backend.environments[2]
    assert "VIRTUAL_ENV" not in backend.environments[3]
    assert backend.environments[1]["PATH"] == "/venv/bin:/usr/bin"
