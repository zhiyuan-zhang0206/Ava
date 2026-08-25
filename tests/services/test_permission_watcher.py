"""macOS permission watcher: log correlation, local lifecycle, and launchd wiring."""

from __future__ import annotations

import json
import logging
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


def test_tcc_parser_prefers_responsible_subject_and_retains_binary_as_tool() -> None:
    parser = watcher.LogEventParser()
    event = parser.parse(
        watcher.PermissionKind.TCC,
        _log_line(
            "AUTHREQ_PROMPTING: msgID=66077.24, "
            "service=kTCCServiceSystemPolicyAppData, "
            f"subject=Sub:{{{_UV_PYTHON}}}Resp:{{TCCDProcess: identifier=-, pid=84991, "
            f"auid=501, euid=501, responsible_path={_UV_PYTHON}, "
            "binary_path=/usr/bin/find}"
        ),
    )

    assert event is not None
    assert event.subject == _UV_PYTHON
    assert event.tool == "/usr/bin/find"


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
    *,
    correlation_id: str = "request-1",
    tool: str | None = None,
) -> watcher.PermissionEvent:
    return watcher.PermissionEvent(kind, phase, subject, when, correlation_id, tool)


def test_new_prompt_logs_info_once_and_repeats_only_refresh_pending(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.DEBUG, logger="ava.permission_watcher")
    service = watcher.PermissionWatcher(tmp_path / "state.json")
    service.observe(
        _event(
            watcher.PermissionKind.TCC,
            watcher.EventPhase.PROMPTING,
            _T0,
            tool="/usr/bin/find",
        )
    )
    repeated_at = _T0 + timedelta(hours=6)
    service.observe(
        _event(
            watcher.PermissionKind.TCC,
            watcher.EventPhase.PROMPTING,
            repeated_at,
            correlation_id="request-2",
            tool="/usr/bin/du",
        )
    )

    assert len(service.pending) == 1
    pending = next(iter(service.pending.values()))
    assert pending.first_seen == _T0
    assert pending.last_seen == repeated_at
    assert pending.correlation_id == "request-2"
    assert pending.tool == "/usr/bin/find"
    info_messages = [
        record.getMessage() for record in caplog.records if record.levelno == logging.INFO
    ]
    assert info_messages == [f"permission prompt: kind=TCC subject={_UV_PYTHON} tool=/usr/bin/find"]


def test_resolution_pops_pending_and_logs_info(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.DEBUG, logger="ava.permission_watcher")
    service = watcher.PermissionWatcher(tmp_path / "state.json")
    service.observe(_event(watcher.PermissionKind.ALF, watcher.EventPhase.PROMPTING, _T0))
    caplog.clear()
    service.observe(
        _event(watcher.PermissionKind.ALF, watcher.EventPhase.RESOLVED, _T0 + timedelta(seconds=10))
    )

    assert service.pending == {}
    assert [record.getMessage() for record in caplog.records if record.levelno == logging.INFO] == [
        f"permission prompt resolved: kind=ALF subject={_UV_PYTHON}"
    ]


def test_escalation_logs_warning_once_per_incident(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.DEBUG, logger="ava.permission_watcher")
    service = watcher.PermissionWatcher(tmp_path / "state.json")
    service.observe(_event(watcher.PermissionKind.TCC, watcher.EventPhase.PROMPTING, _T0))
    caplog.clear()

    service.check_timeouts(_T0 + timedelta(minutes=29, seconds=59))
    service.check_timeouts(_T0 + timedelta(minutes=30))
    service.check_timeouts(_T0 + timedelta(hours=1))

    warning_messages = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert warning_messages == [
        f"permission prompt still pending 30min: kind=TCC subject={_UV_PYTHON}"
    ]
    assert next(iter(service.pending.values())).escalated is True


def test_pending_state_round_trip_omits_notified_and_loads_legacy_shape(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "permission-watcher.json"
    first = watcher.PermissionWatcher(state_path)
    first.observe(
        _event(
            watcher.PermissionKind.TCC,
            watcher.EventPhase.PROMPTING,
            _T0,
            tool="/usr/bin/find",
        )
    )
    first.check_timeouts(_T0 + timedelta(minutes=30))

    state = json.loads(state_path.read_text())
    persisted = next(iter(state["pending"].values()))
    assert "notified" not in persisted

    restarted = watcher.PermissionWatcher(state_path)
    pending = next(iter(restarted.pending.values()))
    assert pending.first_seen == _T0
    assert pending.last_seen == _T0
    assert pending.correlation_id == "request-1"
    assert pending.tool == "/usr/bin/find"
    assert pending.escalated is True

    persisted["notified"] = True
    persisted.pop("tool")
    state_path.write_text(json.dumps(state))

    legacy_restarted = watcher.PermissionWatcher(state_path)
    legacy_pending = next(iter(legacy_restarted.pending.values()))
    assert legacy_pending.first_seen == _T0
    assert legacy_pending.escalated is True
    assert legacy_pending.tool is None


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
