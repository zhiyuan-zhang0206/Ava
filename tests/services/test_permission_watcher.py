"""macOS permission watcher: log correlation, notice lifecycle, and launchd wiring."""

from __future__ import annotations

import json
import plistlib
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import cli.commands._converge as cv
import cli.commands._converge_permission_watcher as converge_watcher
import shared.proc
from services.permission_watcher import watcher

_T0 = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
_UV_PYTHON = "/Users/ava/.local/share/uv/python/cpython-3.12.12/bin/python3.12"


def _log_line(message: str, when: datetime = _T0) -> str:
    return json.dumps({"timestamp": when.isoformat(), "eventMessage": message})


def test_tcc_parser_correlates_attribution_prompt_and_result() -> None:
    parser = watcher.LogEventParser()
    attribution = parser.parse(
        watcher.PermissionKind.TCC,
        _log_line(
            "AUTHREQ_ATTRIBUTION: auth_req=0xabc, responsible={TCCDProcess: "
            f"identifier=com.ava.worker, pid=441, binary_path={_UV_PYTHON}}}"
        ),
    )
    prompting = parser.parse(
        watcher.PermissionKind.TCC,
        _log_line("AUTHREQ_PROMPTING: auth_req=0xabc", _T0 + timedelta(seconds=1)),
    )
    result = parser.parse(
        watcher.PermissionKind.TCC,
        _log_line("AUTHREQ_RESULT: auth_req=0xabc, auth_value=2", _T0 + timedelta(seconds=5)),
    )
    assert attribution == watcher.PermissionEvent(
        watcher.PermissionKind.TCC,
        watcher.EventPhase.ATTRIBUTION,
        _UV_PYTHON,
        _T0,
        "0xabc",
    )
    assert prompting is not None
    assert prompting.phase is watcher.EventPhase.PROMPTING
    assert prompting.subject == _UV_PYTHON
    assert result is not None
    assert result.phase is watcher.EventPhase.RESOLVED
    assert result.subject == _UV_PYTHON


def test_alf_parser_resolves_prompt_pid_and_correlates_verdict() -> None:
    parser = watcher.LogEventParser(pid_resolver=lambda pid: f"/Applications/App-{pid}")
    prompting = parser.parse(
        watcher.PermissionKind.ALF,
        json.dumps(
            {
                "timestamp": _T0.isoformat(),
                "eventMessage": "Prompting for a filtering decision",
                "processPID": 4242,
            }
        ),
    )
    verdict = parser.parse(
        watcher.PermissionKind.ALF,
        _log_line("Found matching app, return known verdict", _T0 + timedelta(seconds=4)),
    )
    assert prompting is not None
    assert prompting.phase is watcher.EventPhase.PROMPTING
    assert prompting.subject == "/Applications/App-4242"
    assert verdict is not None
    assert verdict.phase is watcher.EventPhase.RESOLVED
    assert verdict.subject == prompting.subject


def test_irrelevant_or_malformed_log_lines_are_ignored() -> None:
    parser = watcher.LogEventParser()
    assert parser.parse(watcher.PermissionKind.TCC, "not-json") is None
    assert parser.parse(watcher.PermissionKind.TCC, _log_line("AUTHREQ_RESULTISH")) is None
    assert (
        parser.parse(watcher.PermissionKind.ALF, _log_line("routine socketfilterfw line")) is None
    )


def test_relevant_log_line_with_malformed_timestamp_does_not_kill_parser() -> None:
    parser = watcher.LogEventParser()
    line = json.dumps(
        {
            "timestamp": "not-a-timestamp",
            "eventMessage": "AUTHREQ_PROMPTING: auth_req=0xabc, identifier=com.ava.worker",
        }
    )
    event = parser.parse(watcher.PermissionKind.TCC, line)
    assert event is not None
    assert event.occurred_at.tzinfo is not None


def test_log_stream_commands_subscribe_to_pending_and_resolution_events() -> None:
    commands = watcher.log_stream_commands()
    assert len(commands) == 2
    assert all(
        command[:4] == ["/usr/bin/log", "stream", "--style", "ndjson"] for command in commands
    )
    predicates = [command[command.index("--predicate") + 1] for command in commands]
    assert "AUTHREQ_PROMPTING" in predicates[0]
    assert "AUTHREQ_ATTRIBUTION" in predicates[0]
    assert "AUTHREQ_RESULT" in predicates[0]
    assert 'process == "socketfilterfw"' in predicates[1]
    assert "Prompting" in predicates[1]
    assert "Found matching app, return known verdict" in predicates[1]


def _event(
    kind: watcher.PermissionKind,
    phase: watcher.EventPhase,
    when: datetime,
    subject: str = _UV_PYTHON,
) -> watcher.PermissionEvent:
    return watcher.PermissionEvent(kind, phase, subject, when, "request-1")


def _record_notices(notices: list[tuple[str, str]]) -> watcher.NoticeSender:
    def record(title: str, content: str) -> None:
        notices.append((title, content))

    return record


def test_pending_prompt_is_deduplicated_for_same_subject_within_five_minutes(
    tmp_path: Path,
) -> None:
    notices: list[tuple[str, str]] = []
    service = watcher.PermissionWatcher(tmp_path / "state.json", _record_notices(notices))
    service.observe(_event(watcher.PermissionKind.TCC, watcher.EventPhase.PROMPTING, _T0))
    service.observe(
        _event(
            watcher.PermissionKind.TCC,
            watcher.EventPhase.PROMPTING,
            _T0 + timedelta(minutes=4, seconds=59),
        )
    )
    assert [title for title, _content in notices] == ["macOS 权限弹窗待处理"]
    assert _UV_PYTHON in notices[0][1]
    assert "TCC 完全磁盘访问" in notices[0][1]


def test_prompt_after_five_minutes_of_silence_starts_a_new_dedupe_window(
    tmp_path: Path,
) -> None:
    notices: list[tuple[str, str]] = []
    service = watcher.PermissionWatcher(tmp_path / "state.json", _record_notices(notices))
    service.observe(_event(watcher.PermissionKind.TCC, watcher.EventPhase.PROMPTING, _T0))
    service.observe(
        _event(
            watcher.PermissionKind.TCC,
            watcher.EventPhase.PROMPTING,
            _T0 + timedelta(minutes=5, seconds=1),
        )
    )
    assert [title for title, _content in notices] == [
        "macOS 权限弹窗待处理",
        "macOS 权限弹窗待处理",
    ]


def test_resolution_and_thirty_minute_escalation_close_the_loop(tmp_path: Path) -> None:
    notices: list[tuple[str, str]] = []
    service = watcher.PermissionWatcher(tmp_path / "state.json", _record_notices(notices))
    service.observe(_event(watcher.PermissionKind.ALF, watcher.EventPhase.PROMPTING, _T0))
    service.check_timeouts(_T0 + timedelta(minutes=29, seconds=59))
    assert len(notices) == 1
    service.check_timeouts(_T0 + timedelta(minutes=30))
    assert notices[-1][0] == "macOS 权限弹窗仍未处理"
    assert "已挂起 30 分钟" in notices[-1][1]
    service.observe(
        _event(watcher.PermissionKind.ALF, watcher.EventPhase.RESOLVED, _T0 + timedelta(minutes=31))
    )
    assert notices[-1][0] == "macOS 权限弹窗已处理"
    assert "权限弹窗已处理" in notices[-1][1]
    assert service.pending == {}


def test_pending_state_survives_restart_and_escalates(tmp_path: Path) -> None:
    state_path = tmp_path / "permission-watcher.json"
    first_notices: list[tuple[str, str]] = []
    first = watcher.PermissionWatcher(state_path, _record_notices(first_notices))
    first.observe(_event(watcher.PermissionKind.TCC, watcher.EventPhase.PROMPTING, _T0))

    restarted_notices: list[tuple[str, str]] = []
    restarted = watcher.PermissionWatcher(state_path, _record_notices(restarted_notices))
    assert next(iter(restarted.pending.values())).first_seen == _T0
    restarted.check_timeouts(_T0 + timedelta(minutes=30))
    assert restarted_notices[0][0] == "macOS 权限弹窗仍未处理"


def test_pending_notice_retries_after_database_failure_and_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "permission-watcher.json"

    def fail_notice(_title: str, _content: str) -> None:
        raise RuntimeError("database unavailable")

    first = watcher.PermissionWatcher(state_path, fail_notice)
    with pytest.raises(RuntimeError, match="database unavailable"):
        first.observe(_event(watcher.PermissionKind.TCC, watcher.EventPhase.PROMPTING, _T0))

    notices: list[tuple[str, str]] = []
    restarted = watcher.PermissionWatcher(state_path, _record_notices(notices))
    restarted.check_timeouts(_T0 + timedelta(seconds=1))
    assert [title for title, _content in notices] == ["macOS 权限弹窗待处理"]
    assert next(iter(restarted.pending.values())).notified is True


class _FakeCursor:
    def __init__(self) -> None:
        self.sql = ""
        self.params: tuple[object, ...] = ()

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.sql = sql
        self.params = params

    def fetchone(self) -> tuple[int, int]:
        return (41, 7)


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return self._cursor


def test_notice_insert_uses_bridge_sql_and_fyi_parameters() -> None:
    cursor = _FakeCursor()
    connects: list[tuple[str, dict[str, object]]] = []

    def connect(url: str, **kwargs: object) -> _FakeConnection:
        connects.append((url, kwargs))
        return _FakeConnection(cursor)

    row = watcher.insert_notice(
        "postgresql://ava@127.0.0.1:6433/ava",
        "macOS 权限弹窗待处理",
        "内容",
        connect=connect,
    )
    assert row == (41, 7)
    assert connects == [("postgresql://ava@127.0.0.1:6433/ava", {"connect_timeout": 5})]
    assert "INSERT INTO agent_notices" in cursor.sql
    assert "COALESCE((SELECT MAX(local_id) FROM agent_notices" in cursor.sql
    assert cursor.params == (
        312,
        312,
        "macOS 权限弹窗待处理",
        "内容",
        "P1",
        False,
        False,
        None,
    )


def test_db_url_is_read_from_explicit_prod_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text('OTHER=1\nAVA_DB_URL="postgresql://ava@127.0.0.1:6433/ava"\n')
    assert watcher.read_db_url(env) == "postgresql://ava@127.0.0.1:6433/ava"
    env.write_text("OTHER=1\n")
    with pytest.raises(RuntimeError, match="AVA_DB_URL"):
        watcher.read_db_url(env)


def _ctx(repo: Path, home: Path) -> cv.ConvergeCtx:
    return cv.ConvergeCtx(repo=repo, ava_home=home, roles=frozenset({"gateway"}))


def test_permission_watcher_step_is_host_global_and_gateway_scoped() -> None:
    step = next(
        item
        for item in cv.CONVERGE_STEPS
        if item.apply is converge_watcher.ensure_permission_watcher
    )
    assert step.host_global is True
    assert step.roles == frozenset({"gateway"})
    assert step.requires_unit_config is True


def test_permission_watcher_plist_uses_prod_python_script_and_log(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    home = tmp_path / ".ava"
    plist = plistlib.loads(converge_watcher.render_permission_watcher_plist(_ctx(repo, home)))
    assert plist["Label"] == "com.ava.permission-watcher"
    assert plist["ProgramArguments"] == [
        str(repo / ".venv/bin/python"),
        str(repo / "services/permission_watcher/watcher.py"),
    ]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["StandardOutPath"] == str(home / "logs/permission-watcher.log")
    assert plist["StandardErrorPath"] == str(home / "logs/permission-watcher.log")


def test_permission_watcher_converge_writes_bootstraps_once_then_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setattr(converge_watcher.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(converge_watcher, "launch_agents_dir", lambda: agents)
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(shared.proc, "run_bounded", run)
    context = _ctx(tmp_path / "source", tmp_path / ".ava")
    converge_watcher.ensure_permission_watcher(context)
    plist_path = agents / "com.ava.permission-watcher.plist"
    first_bytes = plist_path.read_bytes()
    converge_watcher.ensure_permission_watcher(context)
    assert plist_path.read_bytes() == first_bytes
    assert [command[1] for command in calls] == ["bootout", "bootstrap"]


def test_permission_watcher_converge_reloads_changed_plist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.ava.permission-watcher.plist").write_bytes(b"stale")
    monkeypatch.setattr(converge_watcher.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(converge_watcher, "launch_agents_dir", lambda: agents)
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(
        shared.proc,
        "run_bounded",
        run,
    )
    converge_watcher.ensure_permission_watcher(_ctx(tmp_path / "source", tmp_path / ".ava"))
    assert [command[1] for command in calls] == ["bootout", "bootstrap"]
    assert plistlib.loads((agents / "com.ava.permission-watcher.plist").read_bytes())["Label"] == (
        "com.ava.permission-watcher"
    )


def test_permission_watcher_failed_bootstrap_is_retryable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setattr(converge_watcher.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(converge_watcher, "launch_agents_dir", lambda: agents)
    bootstrap_attempts = 0

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal bootstrap_attempts
        returncode = 0
        stderr = b""
        if command[1] == "bootstrap":
            bootstrap_attempts += 1
            if bootstrap_attempts == 1:
                returncode = 5
                stderr = b"load failed"
        return subprocess.CompletedProcess(command, returncode, b"", stderr)

    monkeypatch.setattr(shared.proc, "run_bounded", run)
    context = _ctx(tmp_path / "source", tmp_path / ".ava")
    with pytest.raises(RuntimeError, match="load failed"):
        converge_watcher.ensure_permission_watcher(context)
    assert not (agents / "com.ava.permission-watcher.plist").exists()

    converge_watcher.ensure_permission_watcher(context)
    assert bootstrap_attempts == 2


def test_permission_watcher_converge_skips_non_macos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setattr(converge_watcher.platform, "system", lambda: "Linux")
    monkeypatch.setattr(converge_watcher, "launch_agents_dir", lambda: agents)

    def fail_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        pytest.fail("launchctl called on non-macOS")

    monkeypatch.setattr(
        shared.proc,
        "run_bounded",
        fail_run,
    )
    converge_watcher.ensure_permission_watcher(_ctx(tmp_path / "source", tmp_path / ".ava"))
    assert not agents.exists()
