"""Skill-level contracts for isolated Codex launch and terminal supervision."""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

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


spawn_codex = _load("spawn_codex_under_test", _REFERENCE / "spawn_codex.py")
watch_work = _load("watch_work_under_test", _REFERENCE / "watch_work.py")


def _owner(tmp_path: Path) -> coding_session_owner.CodingSessionOwner:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    key = coding_session_owner.canonical_key(
        workspace,
        tool="codex",
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
        expected_suffix="codex-workspace-11111111",
        session_id=7,
        session_name="ava-agent-41-shell-7-codex-workspace-11111111",
        state_dir=coding_session_owner.generation_state_dir(key, generation),
        tasks_file=workspace / "tasks.md",
        work_file=workspace / "work.md",
        created_at=now,
        expires_at=now + dt.timedelta(hours=4),
    )


def test_codex_home_seeds_only_auth_and_config(tmp_path: Path) -> None:
    source = tmp_path / "shared-codex"
    source.mkdir()
    (source / "auth.json").write_text('{"token":"test"}')
    (source / "config.toml").write_text('model = "gpt-test"\n')
    (source / "state_5.sqlite").write_text("shared mutable database")
    (source / "sessions").mkdir()
    (source / "sessions" / "old.jsonl").write_text("old transcript")
    target = tmp_path / "isolated"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    spawn_codex._seed_codex_home(target, workspace, source_home=source)

    assert sorted(path.name for path in target.iterdir()) == ["auth.json", "config.toml"]
    assert (target / "auth.json").read_text() == '{"token":"test"}'
    config = (target / "config.toml").read_text()
    assert 'model = "gpt-test"' in config
    assert f'[projects."{workspace.as_posix()}"]' in config
    assert not (target / "state_5.sqlite").exists()
    assert not (target / "sessions").exists()


def test_launch_command_uses_isolated_home_without_sqlite_resume(tmp_path: Path) -> None:
    record = _owner(tmp_path)

    command = spawn_codex._codex_command(record, Path(record.key.workspace))

    assert command.startswith(f"cd {record.key.workspace} && ")
    assert f"CODEX_HOME={record.state_dir}" in command
    assert "exec codex --dangerously-bypass-approvals-and-sandbox" in command
    assert "resume" not in command
    assert "AVA_CALLER_IDENTITY" not in command


def test_launch_command_can_explicitly_declare_external_caller(tmp_path: Path) -> None:
    record = _owner(tmp_path)
    command = spawn_codex._codex_command(record, Path(record.key.workspace), "run-42")
    assert "AVA_CALLER_IDENTITY=" in command
    assert '"kind":"external_agent"' in command
    assert '"subject":"codex"' in command
    assert '"instance":"run-42"' in command


def test_fresh_launch_publishes_full_handle_and_durable_context(tmp_path: Path) -> None:
    record = _owner(tmp_path)
    workspace = Path(record.key.workspace)
    tasks_file = workspace / "tasks.md"
    work_file = workspace / "work.md"

    assert (
        coding_session_owner.full_session_name(41, 7, "codex-workspace-11111111")
        == "ava-agent-41-shell-7-codex-workspace-11111111"
    )
    message = spawn_codex._bootstrap_message(workspace, tasks_file, work_file)
    assert str(spawn_codex._contract_path()) in message
    assert str(workspace) in message
    assert str(tasks_file) in message
    assert str(work_file) in message


def test_supervisor_bootstrap_restores_owner_identity(tmp_path: Path) -> None:
    code = spawn_codex._supervisor_code(_owner(tmp_path))

    assert "os.environ['AVA_AGENT_ID'] = '41'" in code
    assert "['watch']" in code


def test_failed_early_publish_kills_codex_session_before_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _owner(tmp_path)
    launching = replace(active, status="launching", session_id=None, session_name=None)
    events: list[str] = []
    killed: list[int] = []

    def _claim(
        _key: coding_session_owner.CodingSessionKey,
        *,
        tasks_file: Path,
        work_file: Path,
        ttl_seconds: float,
    ) -> coding_session_owner.CodingSessionClaim:
        assert tasks_file.name == "tasks.md"
        assert work_file.name == "work.md"
        assert ttl_seconds == 3600
        events.append("claim")
        return coding_session_owner.CodingSessionClaim(action="launch", owner=launching)

    def _seed(_state_dir: Path, _workspace: Path) -> None:
        return None

    def _launch_supervisor(
        _owner: coding_session_owner.CodingSessionOwner,
        _ttl_seconds: float,
    ) -> tuple[int, str]:
        return 6, "ava-agent-41-shell-6-codex-owner-supervisor"

    def _attach(
        _key: coding_session_owner.CodingSessionKey,
        _generation: str,
        *,
        session_id: int,
        session_name: str,
    ) -> coding_session_owner.CodingSessionOwner:
        assert session_id == 6
        assert session_name.endswith("-codex-owner-supervisor")
        events.append("attach")
        return launching

    def _new(*, name: str, ttl: float) -> int:
        assert name == launching.expected_suffix
        assert ttl == 3600
        events.append("new")
        return 7

    def _send(_session_id: int, _content: str) -> None:
        events.append("send")

    def _ready(_session_id: int) -> None:
        events.append("ready")

    def _verified(_sid: int, _codex_home: Path) -> None:
        events.append("verified")

    def _publish(
        _key: coding_session_owner.CodingSessionKey,
        _generation: str,
        *,
        session_id: int,
        session_name: str,
    ) -> coding_session_owner.CodingSessionOwner:
        assert session_id == 7
        assert session_name.endswith("-codex-workspace-11111111")
        events.append("publish")
        raise coding_session_owner.CodingSessionGenerationChangedError("replacement won")

    def _kill(session_id: int) -> None:
        killed.append(session_id)

    def _terminate(
        _key: coding_session_owner.CodingSessionKey,
        _generation: str,
        *,
        reason: str,
    ) -> bool:
        assert reason == "launch-failed"
        return False

    monkeypatch.setattr(spawn_codex, "_claim_canonical", _claim)
    monkeypatch.setattr(spawn_codex, "_seed_codex_home", _seed)
    monkeypatch.setattr(spawn_codex, "_launch_supervisor", _launch_supervisor)
    monkeypatch.setattr(spawn_codex.coding_session_owner, "attach_supervisor", _attach)
    monkeypatch.setattr(spawn_codex.ava.shell.sessions, "new", _new)
    monkeypatch.setattr(spawn_codex.ava.shell.sessions, "send", _send)
    monkeypatch.setattr(spawn_codex, "_wait_for_ready", _ready)
    monkeypatch.setattr(spawn_codex, "_verify_submitted", _verified)
    monkeypatch.setattr(spawn_codex.coding_session_owner, "publish_active", _publish)
    monkeypatch.setattr(spawn_codex.ava.shell.sessions, "kill", _kill)
    monkeypatch.setattr(spawn_codex.coding_session_owner, "terminate_generation", _terminate)

    workspace = Path(launching.key.workspace)
    with pytest.raises(coding_session_owner.CodingSessionGenerationChangedError):
        spawn_codex._launch(
            workspace,
            workspace / "tasks.md",
            workspace / "work.md",
            3600,
        )

    assert events == ["claim", "attach", "new", "publish"]
    assert killed == [7]


@pytest.mark.parametrize(
    ("status", "kwargs", "expected"),
    [
        ("DONE", {"status_is_current": True}, "collaboration-done"),
        ("HANDOFF", {"status_is_current": True}, "collaboration-handoff"),
        ("WORKING", {"owner_terminated": True}, "owner-terminated"),
        ("WORKING", {"session_crashed": True}, "session-crashed"),
        ("WORKING", {"expired": True}, "expired"),
    ],
)
def test_terminal_reason_covers_every_supervised_lifecycle(
    status: str,
    kwargs: dict[str, bool],
    expected: str,
) -> None:
    inputs = {
        "status_is_current": False,
        "owner_terminated": False,
        "session_crashed": False,
        "expired": False,
        "work_file_deleted": False,
        "hard_limit_reached": False,
    }
    inputs.update(kwargs)

    assert (
        watch_work.terminal_reason(
            status,
            status_is_current=inputs["status_is_current"],
            owner_terminated=inputs["owner_terminated"],
            session_crashed=inputs["session_crashed"],
            expired=inputs["expired"],
            work_file_deleted=inputs["work_file_deleted"],
            hard_limit_reached=inputs["hard_limit_reached"],
        )
        == expected
    )


@pytest.mark.parametrize(
    ("status", "owner_dead", "crashed", "expired", "expected"),
    [
        ("DONE", False, False, False, "collaboration-done"),
        ("HANDOFF", False, False, False, "collaboration-handoff"),
        ("WORKING", True, False, False, "owner-terminated"),
        ("WORKING", False, True, False, "session-crashed"),
        ("WORKING", False, False, True, "expired"),
    ],
)
def test_canonical_watch_terminalizes_before_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    owner_dead: bool,
    crashed: bool,
    expired: bool,
    expected: str,
) -> None:
    record = _owner(tmp_path)
    work_file = Path(record.work_file or "")
    work_file.write_text(f"STATUS: {status}\n")
    if expired:
        record = replace(
            record,
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
    calls: list[tuple[str, str]] = []

    def _read(_key: coding_session_owner.CodingSessionKey) -> Any:
        return record

    def _terminalize(
        _key: coding_session_owner.CodingSessionKey,
        generation: str,
        _owner_agent_id: int,
        reason: str,
    ) -> bool:
        calls.append((generation, reason))
        return True

    def _owner_dead(_agent_id: int) -> bool:
        return owner_dead

    def _crashed(_owner: coding_session_owner.CodingSessionOwner) -> bool:
        return crashed

    monkeypatch.setattr(watch_work.coding_session_owner, "read", _read)
    monkeypatch.setattr(watch_work, "_owner_terminated", _owner_dead)
    monkeypatch.setattr(watch_work, "_session_crashed", _crashed)
    monkeypatch.setattr(watch_work, "_terminalize", _terminalize)

    watch_work.watch(
        str(work_file),
        cluster=record.key.cluster,
        workspace=record.key.workspace,
        generation=record.generation,
        owner_agent_id=record.owner_agent_id,
    )

    assert calls == [(record.generation, expected)]


def test_canonical_notifications_never_resurrect_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes: list[tuple[int, str, str, bool]] = []

    def _note(agent_id: int, content: str, *, tag: str, resurrect: bool) -> int:
        notes.append((agent_id, content, tag, resurrect))
        return 1

    def _message(_agent_id: int, _content: str) -> None:
        raise AssertionError("canonical supervisor must not use resurrecting peer messages")

    monkeypatch.setattr(watch_work.ava.agents, "send_system_note", _note)
    monkeypatch.setattr(watch_work.ava.agents, "send_message", _message)

    watch_work._notify(41, "terminal", canonical=True)

    assert notes == [(41, "terminal", "task", False)]
