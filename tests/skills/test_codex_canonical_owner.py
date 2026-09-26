"""Skill-level contracts for isolated Codex launch and terminal supervision."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
import time
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from shared import coding_session_owner
from shared.platform import IS_WINDOWS

_REFERENCE = (
    Path(__file__).parents[2] / "ava_builtins" / "skills" / "ava-use-other-agents" / "reference"
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


def _record_app_server(events: list[str], _endpoint: str, *, log_path: Path | None = None) -> None:
    """The takeover's readiness wait records as an event; the launch hands it the server log."""
    assert log_path is not None and log_path.name == "app-server.log"
    events.append("app-server")


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


@pytest.mark.skipif(IS_WINDOWS, reason="PTY sessions require POSIX")
def test_codex_supervisor_uses_projected_session_environment(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner's real sessions.new path must remove an inherited foreign venv."""
    from ava.shell import sessions
    from shared.session_backend import PtySessionBackend

    owner = _owner(unit_home)
    workspace = Path(owner.key.workspace)
    backend = PtySessionBackend()
    report = unit_home / "codex-child-env.json"
    # unit_home pins in-process Settings; subprocesses read the raw env at boot.
    monkeypatch.setitem(os.environ, "AVA_HOME", str(unit_home))
    monkeypatch.setenv("HOME", str(unit_home))
    monkeypatch.setenv("VIRTUAL_ENV", str(unit_home / "foreign" / ".venv"))
    monkeypatch.setattr(spawn_codex.ava._boot, "_agent_id", 41)
    monkeypatch.setattr(sessions, "_next_session_index_from_db", lambda: 7)
    monkeypatch.setattr(sessions, "_shell_prefix", lambda: "ava-agent-41-shell-")

    def workspace_for_owner(_agent_id: int) -> Path:
        return workspace

    def record_no_ttl(_sid: int, _ttl: float) -> None:
        return None

    monkeypatch.setattr(sessions, "workspace_dir", workspace_for_owner)
    monkeypatch.setattr(sessions, "get_shell_backend", lambda: backend)
    monkeypatch.setattr(sessions, "_record_ttl", record_no_ttl)

    # Execute a probe in place of the long-running supervisor; session birth,
    # envfile transport, host fork, and shell command delivery remain real.
    def supervisor_probe(_owner: coding_session_owner.CodingSessionOwner) -> str:
        return (
            "import json, os; from pathlib import Path; "
            f"report = Path({str(report)!r}); pending = report.with_suffix('.tmp'); "
            "pending.write_text(json.dumps(dict("
            "virtual_env=os.environ.get('VIRTUAL_ENV'), cwd=os.getcwd()))); pending.replace(report)"
        )

    monkeypatch.setattr(spawn_codex, "_supervisor_code", supervisor_probe)
    name = coding_session_owner.full_session_name(41, 7, spawn_codex._supervisor_name(owner))
    try:
        sid, actual_name = spawn_codex._launch_supervisor(owner, 120)
        assert (sid, actual_name) == (7, name)
        deadline = time.monotonic() + 15
        while not report.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert report.exists()
        assert json.loads(report.read_text()) == {"virtual_env": None, "cwd": str(workspace)}
    finally:
        backend.kill_session(name)


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
        supervisor_session_id=None,
        supervisor_session_name=None,
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

    def _seed(_state_dir: Path, _workspace: Path) -> None:
        events.append("seed")

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

    def _verified(_session_id: int, _codex_home: Path) -> None:
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
        return replace(launching, status="active", session_id=7, session_name=session_name)

    monkeypatch.setattr(spawn_codex, "_claim_canonical", _claim)
    monkeypatch.setattr(spawn_codex, "_init_file", _unexpected)
    monkeypatch.setattr(spawn_codex, "_launch_supervisor", _unexpected)
    monkeypatch.setattr(spawn_codex.coding_session_owner, "attach_supervisor", _unexpected)
    monkeypatch.setattr(spawn_codex, "_seed_codex_home", _seed)
    monkeypatch.setattr(spawn_codex, "_wait_for_app_server", partial(_record_app_server, events))
    monkeypatch.setattr(spawn_codex.ava.shell.sessions, "new", _new)
    monkeypatch.setattr(spawn_codex.ava.shell.sessions, "send", _send)
    monkeypatch.setattr(spawn_codex, "_wait_for_ready", _ready)
    monkeypatch.setattr(spawn_codex, "_verify_submitted", _verified)
    monkeypatch.setattr(spawn_codex.coding_session_owner, "publish_active", _publish)

    workspace = Path(launching.key.workspace)
    brief = "Goal: replace the agent. The briefing is inline; read no files."
    rc = spawn_codex._launch(workspace, None, None, 3600, None, "Fix login", brief)

    assert rc == 0
    assert events == [
        "claim",
        "seed",
        "new",
        "publish",
        "send",
        "app-server",
        "send",
        "ready",
        "send",
        "verified",
    ]
    assert "codex app-server --listen" in sent[0]
    assert sent[1].startswith("clear && ") and "exec codex --remote unix://" in sent[1]
    assert "take over Ava agent 41" in sent[2]
    assert brief in sent[2]
    assert "tasks.md" not in sent[2] and "work.md" not in sent[2]


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

    monkeypatch.setattr(spawn_codex, "_claim_canonical", _claim)

    with pytest.raises(RuntimeError, match="fresh coding workspace"):
        spawn_codex._launch(
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
def test_codex_takeover_cli_rejects_files_and_requires_a_brief(
    extra: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ava._boot.require_agent_id", lambda: 41)
    monkeypatch.setattr(
        sys,
        "argv",
        ["spawn_codex.py", str(tmp_path), "--impersonate-self", *extra],
    )

    with pytest.raises(SystemExit):
        spawn_codex.main()


def test_codex_brief_requires_takeover_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["spawn_codex.py", str(tmp_path), "--brief", "briefing"])

    with pytest.raises(SystemExit):
        spawn_codex.main()


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


# ── Receipt checkpoints: a dead session must warn, not roll the launch back ──


def test_submission_check_survives_a_dead_session_at_enter(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Enter retry on a dead pane warns; it must not raise (rollback class)."""

    def no_sleep(_seconds: float) -> None:
        return None

    def idle_capture(_sid: int, **_kwargs: object) -> str:
        return "composer idle"

    def dead_keys(_sid: int, *_keys: str) -> None:
        raise ValueError("session 7 is not this agent's (no match for 'shell-7')")

    monkeypatch.setattr(spawn_codex.time, "sleep", no_sleep)
    monkeypatch.setattr(spawn_codex.ava.shell.sessions, "capture", idle_capture)
    monkeypatch.setattr(spawn_codex.ava.shell.sessions, "send_keys", dead_keys)

    spawn_codex._verify_submitted(7, Path("/nonexistent"), timeout=0.01)
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "Enter retry failed" in out


def test_submission_check_survives_a_dead_session_at_capture(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The polling capture read of a dead pane warns instead of raising."""

    def dead_capture(_sid: int, **_kwargs: object) -> str:
        raise ValueError("session 7 is not this agent's (no match for 'shell-7')")

    monkeypatch.setattr(spawn_codex.ava.shell.sessions, "capture", dead_capture)

    spawn_codex._verify_submitted(7, Path("/nonexistent"), timeout=5.0)
    assert "capture failed" in capsys.readouterr().out


def test_submission_check_survives_a_dead_session_after_the_enter_retry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The post-Enter capture read of a dead pane warns instead of raising.

    The pane looks alive (idle composer) until the Enter retry lands, so the
    refusal surfaces only at the re-check — the second capture site.
    """

    def no_sleep(_seconds: float) -> None:
        return None

    entered = {"sent": False}

    def capture(_sid: int, **_kwargs: object) -> str:
        if entered["sent"]:
            raise ValueError("session 7 is not this agent's (no match for 'shell-7')")
        return "composer idle"

    def keys(_sid: int, *_keys: str) -> None:
        entered["sent"] = True

    monkeypatch.setattr(spawn_codex.time, "sleep", no_sleep)
    monkeypatch.setattr(spawn_codex.ava.shell.sessions, "capture", capture)
    monkeypatch.setattr(spawn_codex.ava.shell.sessions, "send_keys", keys)

    spawn_codex._verify_submitted(7, Path("/nonexistent"), timeout=0.01)
    assert "capture failed" in capsys.readouterr().out
