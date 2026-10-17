from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from base.sessions import coding_session_owner
from base.sessions.coding_session_owner_record import CodingSessionStatus
from meiva.sdk_surface import agent_identity
from meiva.shell.coding_tools import antigravity, claude

_SKILL_DIR = (
    Path(__file__).parents[4]
    / "meiva_builtins"
    / "skills"
    / "platform"
    / "meiva-guide"
    / "external-agents"
)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spawn_claude = _load("spawn_claude_under_test", _SKILL_DIR / "scripts" / "spawn_claude.py")
spawn_antigravity = _load(
    "spawn_antigravity_under_test", _SKILL_DIR / "scripts" / "spawn_antigravity.py"
)


def _owner(tmp_path: Path) -> coding_session_owner.CodingSessionOwner:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    key = coding_session_owner.canonical_key(
        workspace,
        tool="claude",
        cluster=tmp_path / "cluster",
    )
    generation = "11111111-2222-3333-4444-555555555555"
    now = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    return coding_session_owner.CodingSessionOwner(
        key=key,
        status=CodingSessionStatus.ACTIVE,
        generation=generation,
        owner_agent_id=41,
        display_label="workspace",
        expected_suffix="claude-workspace-11111111",
        session_id=7,
        session_name="meiva-agent-41-shell-7-claude-workspace-11111111",
        state_dir=coding_session_owner.generation_state_dir(key, generation),
        tasks_file=None,
        work_file=None,
        created_at=now,
        expires_at=now + dt.timedelta(hours=24),
    )


def test_launch_command_unsets_api_key_and_skips_permission_prompts(tmp_path: Path) -> None:
    record = _owner(tmp_path)

    command = claude.claude_command(Path(record.key.workspace))

    assert command.startswith(f"cd {record.key.workspace} && ")
    assert "unset ANTHROPIC_API_KEY && " in command
    assert command.endswith('exec "$claude_bin" --dangerously-skip-permissions || exit $?')
    assert "MEIVA_CALLER_IDENTITY" not in command


def test_launch_command_can_explicitly_declare_external_caller(tmp_path: Path) -> None:
    record = _owner(tmp_path)

    command = claude.claude_command(Path(record.key.workspace), "run-42")

    assert "MEIVA_CALLER_IDENTITY=" in command
    assert '"kind":"external_agent"' in command
    assert '"subject":"claude_code"' in command
    assert '"instance":"run-42"' in command


def test_prompt_launch_sends_prompt_without_creating_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    sent: list[str] = []

    def _unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a prompt launch must not create task or work files")

    def _pretrust(_workspace: Path) -> None:
        events.append("pretrust")

    def _new(*, name: str, ttl: float) -> int:
        assert name.startswith("claude-")
        assert ttl == 3600
        events.append("new")
        return 7

    def _send(session_id: int, content: str) -> None:
        assert session_id == 7
        sent.append(content)
        events.append("send")

    def _ready(_session_id: int, **_kwargs: object) -> None:
        events.append("ready")

    monkeypatch.setattr(claude, "init_file", _unexpected)
    monkeypatch.setattr(claude, "pretrust", _pretrust)
    monkeypatch.setattr(claude.sessions, "new", _new)
    monkeypatch.setattr(claude.sessions, "send", _send)
    monkeypatch.setattr(claude, "wait_for_ready", _ready)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt = "Please run meiva impersonate request --agent 41 --name 'Fix login' --provider claude"
    rc = claude.launch(
        workspace,
        None,
        None,
        3600,
        None,
        skill_dir=_SKILL_DIR,
        prompt=prompt,
        preset_first_run=False,
        check_session_exists=lambda _name: False,
    )

    assert rc == 0
    assert events == ["pretrust", "new", "send", "ready", "send"]
    assert sent[0].startswith(f"cd {workspace.as_posix()} && ")
    assert sent[1] == prompt


@pytest.mark.parametrize(
    "extra",
    [
        ["--prompt", "hello", "--tasks-file", "tasks.md"],
        ["--prompt", "hello", "--work-file", "work.md"],
        ["--prompt", "   "],
    ],
)
def test_claude_prompt_cli_rejects_files_and_blank_prompt(
    extra: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_identity, "require_agent_id", lambda: 41)
    monkeypatch.setattr(
        sys,
        "argv",
        ["spawn_claude.py", str(tmp_path), *extra],
    )

    with pytest.raises(SystemExit):
        spawn_claude.main()


def test_supervised_launch_needs_its_files_or_prompt(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="a supervised launch needs its task and work files"):
        claude.launch(tmp_path, None, None, 3600, skill_dir=_SKILL_DIR, preset_first_run=False)


def test_antigravity_launch_needs_its_files_or_prompt(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="a supervised launch needs its task and work files"):
        antigravity.launch(tmp_path, None, None, skill_dir=_SKILL_DIR)


def test_antigravity_prompt_cli_rejects_files_and_blank_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["spawn_antigravity.py", str(tmp_path), "--prompt", "   "],
    )

    with pytest.raises(SystemExit):
        spawn_antigravity.main()
