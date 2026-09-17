"""Skill-level contracts for the isolated Claude Code launch and its file-less takeover."""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from shared import coding_session_owner

_REFERENCE = (
    Path(__file__).parents[2]
    / "ava_builtins"
    / "skills"
    / "ava-use-claude-code-and-codex"
    / "reference"
)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spawn_claude = _load("spawn_claude_under_test", _REFERENCE / "spawn_claude.py")


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
        status="active",
        generation=generation,
        owner_agent_id=41,
        display_label="workspace",
        expected_suffix="claude-workspace-11111111",
        session_id=7,
        session_name="ava-agent-41-shell-7-claude-workspace-11111111",
        state_dir=coding_session_owner.generation_state_dir(key, generation),
        tasks_file=None,
        work_file=None,
        created_at=now,
        expires_at=now + dt.timedelta(hours=24),
    )


def test_launch_command_unsets_api_key_and_skips_permission_prompts(tmp_path: Path) -> None:
    record = _owner(tmp_path)

    command = spawn_claude._claude_command(Path(record.key.workspace))

    assert command.startswith(f"cd {record.key.workspace} && ")
    assert "unset ANTHROPIC_API_KEY && " in command
    assert command.endswith("claude --dangerously-skip-permissions")
    assert "AVA_CALLER_IDENTITY" not in command


def test_launch_command_can_explicitly_declare_external_caller(tmp_path: Path) -> None:
    record = _owner(tmp_path)

    command = spawn_claude._claude_command(Path(record.key.workspace), "run-42")

    assert "AVA_CALLER_IDENTITY=" in command
    assert '"kind":"external_agent"' in command
    assert '"subject":"claude_code"' in command
    assert '"instance":"run-42"' in command


def test_takeover_launch_inlines_brief_without_files_or_supervisor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _owner(tmp_path)
    launching = replace(
        active,
        status="launching",
        session_id=None,
        session_name=None,
        tasks_file=None,
        work_file=None,
    )
    events: list[str] = []
    sent: list[str] = []

    def _claim(
        _key: coding_session_owner.CodingSessionKey,
        *,
        tasks_file: Path | None,
        work_file: Path | None,
        ttl_seconds: float,
    ) -> coding_session_owner.CodingSessionClaim:
        assert tasks_file is None and work_file is None
        assert ttl_seconds == 3600
        events.append("claim")
        return coding_session_owner.CodingSessionClaim(action="launch", owner=launching)

    def _unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a takeover launch must not create files or start a supervisor")

    def _pretrust(_workspace: Path) -> None:
        events.append("pretrust")

    def _new(*, name: str, ttl: float) -> int:
        assert name == launching.expected_suffix
        assert ttl == 3600
        events.append("new")
        return 7

    def _send(session_id: int, content: str) -> None:
        assert session_id == 7
        sent.append(content)
        events.append("send")

    def _ready(_session_id: int) -> None:
        events.append("ready")

    def _receipt(_session_id: int) -> None:
        events.append("receipt")

    def _publish(
        _key: coding_session_owner.CodingSessionKey,
        _generation: str,
        *,
        session_id: int,
        session_name: str,
    ) -> coding_session_owner.CodingSessionOwner:
        assert session_id == 7
        assert session_name.endswith("-claude-workspace-11111111")
        events.append("publish")
        return replace(launching, status="active", session_id=7, session_name=session_name)

    monkeypatch.setattr(spawn_claude, "_claim_canonical", _claim)
    monkeypatch.setattr(spawn_claude, "_init_file", _unexpected)
    monkeypatch.setattr(spawn_claude, "_pretrust", _pretrust)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "new", _new)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send", _send)
    monkeypatch.setattr(spawn_claude, "_wait_for_ready", _ready)
    monkeypatch.setattr(spawn_claude, "_verify_start_receipt", _receipt)
    monkeypatch.setattr(spawn_claude.coding_session_owner, "publish_active", _publish)

    workspace = Path(launching.key.workspace)
    brief = "Goal: replace the agent. The briefing is inline; read no files."
    rc = spawn_claude._launch(workspace, None, None, 3600, None, "Fix login", brief)

    assert rc == 0
    assert events == ["claim", "pretrust", "new", "publish", "send", "ready", "send", "receipt"]
    assert sent[0].startswith(f"cd {workspace.as_posix()} && ")
    message = sent[1]
    assert "take over Ava agent 41" in message
    assert brief in message
    assert "tasks.md" not in message and "work.md" not in message


def test_takeover_launch_refuses_a_workspace_with_a_live_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _owner(tmp_path)

    def _claim(
        _key: coding_session_owner.CodingSessionKey,
        *,
        tasks_file: Path | None,
        work_file: Path | None,
        ttl_seconds: float,
    ) -> coding_session_owner.CodingSessionClaim:
        assert tasks_file is None and work_file is None
        return coding_session_owner.CodingSessionClaim(action="adopt", owner=record)

    monkeypatch.setattr(spawn_claude, "_claim_canonical", _claim)

    with pytest.raises(RuntimeError, match="fresh coding workspace"):
        spawn_claude._launch(
            Path(record.key.workspace), None, None, 3600, None, "Fix login", "the briefing"
        )


@pytest.mark.parametrize(
    "extra",
    [
        ["--brief", "briefing", "--tasks-file", "tasks.md"],
        ["--brief", "briefing", "--work-file", "work.md"],
        [],
        ["--brief", "   "],
    ],
)
def test_claude_takeover_cli_rejects_files_and_requires_a_brief(
    extra: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ava._boot.require_agent_id", lambda: 41)
    monkeypatch.setattr(
        sys,
        "argv",
        ["spawn_claude.py", str(tmp_path), "--impersonate-self", *extra],
    )

    with pytest.raises(SystemExit):
        spawn_claude.main()


def test_claude_brief_requires_takeover_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["spawn_claude.py", str(tmp_path), "--brief", "briefing"])

    with pytest.raises(SystemExit):
        spawn_claude.main()


def test_supervised_launch_needs_its_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="supervised launch needs its task and work files"):
        spawn_claude._launch(tmp_path, None, None, 3600)
