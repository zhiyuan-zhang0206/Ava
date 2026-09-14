"""Self-takeover bootstrap names the launching agent and validates before launch."""

import importlib.util
import sys
from pathlib import Path

import pytest

from ava._impersonation_launch import bootstrap_message

_REFERENCE = (
    Path(__file__).parents[2] / "ava_builtins/skills/ava-use-claude-code-and-codex/reference"
)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_self_takeover_bootstrap_links_real_guide_and_separate_handles(
    provider: str, tmp_path: Path
) -> None:
    guide = _REFERENCE.parents[3] / ".agents/skills/impersonator-guide/SKILL.md"
    assert guide.is_file()
    brief = tmp_path / "tasks with spaces.md"
    message = bootstrap_message(42, "Fix login", provider, brief, tmp_path / "work.md", guide)
    assert "take over Ava agent 42" in message
    assert "--agent 42" in message and "--name 'Fix login'" in message
    assert str(guide) in message and str(brief) in message
    assert "ava impersonate say" in message
    assert "release with your own summary" in message
    if provider == "codex":
        assert "CODEX_THREAD_ID" in message and "CODEX_HOME" in message
    else:
        assert "Monitor relay with --session" in message


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_launch_requires_native_identity_before_creating_workspace(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location(
        f"takeover_spawn_{provider}", _REFERENCE / f"spawn_{provider}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "must-not-be-created"
    monkeypatch.setattr(sys, "argv", [f"spawn_{provider}.py", str(target), "--impersonate-self"])

    def no_identity() -> None:
        raise RuntimeError("No launching Ava identity")

    monkeypatch.setattr("ava._boot.require_agent_id", no_identity)
    with pytest.raises(RuntimeError, match="No launching Ava identity"):
        module.main()
    assert not target.exists()
