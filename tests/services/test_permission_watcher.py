"""macOS permission watcher: log correlation, local lifecycle, and launchd wiring."""

from __future__ import annotations

import json
import logging
import plistlib
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
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


def _only_alert(payload: dict[str, object]) -> dict[str, object]:
    return cast(list[dict[str, object]], payload["alerts"])[0]


def test_new_prompt_posts_one_firing_alert_and_repeats_only_refresh_pending(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.DEBUG, logger="ava.permission_watcher")
    posts: list[dict[str, object]] = []
    service = watcher.PermissionWatcher(tmp_path / "state.json", posts.append)
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
    assert pending.mode == "full"
    assert pending.alert_posted is True
    assert len(posts) == 1
    assert posts[0]["source"] == "permission-watcher"
    assert posts[0]["status"] == "firing"
    alert = _only_alert(posts[0])
    assert alert["status"] == "firing"
    assert alert["labels"] == {
        "alertname": "permission-prompt",
        "severity": "warning",
        "kind": "TCC",
        "subject": _UV_PYTHON,
    }
    assert alert["startsAt"] == _T0.isoformat()
    assert alert["endsAt"] == ""
    summary = cast(dict[str, str], alert["annotations"])["summary"]
    assert _UV_PYTHON in summary
    assert "/usr/bin/find" in summary
    info_messages = [
        record.getMessage() for record in caplog.records if record.levelno == logging.INFO
    ]
    assert info_messages == [f"permission prompt: kind=TCC subject={_UV_PYTHON} tool=/usr/bin/find"]


def test_resolution_posts_same_alert_instance_and_records_resolution(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.DEBUG, logger="ava.permission_watcher")
    posts: list[dict[str, object]] = []
    service = watcher.PermissionWatcher(tmp_path / "state.json", posts.append)
    service.observe(_event(watcher.PermissionKind.ALF, watcher.EventPhase.PROMPTING, _T0))
    caplog.clear()
    resolved_at = _T0 + timedelta(seconds=10)
    service.observe(_event(watcher.PermissionKind.ALF, watcher.EventPhase.RESOLVED, resolved_at))

    assert service.pending == {}
    assert service.resolved == {f"ALF:{_UV_PYTHON}": resolved_at}
    assert len(posts) == 2
    firing = _only_alert(posts[0])
    resolved = _only_alert(posts[1])
    assert posts[1]["status"] == "resolved"
    assert resolved["status"] == "resolved"
    assert resolved["labels"] == firing["labels"]
    assert resolved["startsAt"] == firing["startsAt"] == _T0.isoformat()
    assert resolved["endsAt"] == resolved_at.isoformat()
    assert [record.getMessage() for record in caplog.records if record.levelno == logging.INFO] == [
        f"permission prompt resolved: kind=ALF subject={_UV_PYTHON}"
    ]


def test_resolved_cooldown_tracks_silent_recurrence_then_allows_new_alert(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.DEBUG, logger="ava.permission_watcher")
    posts: list[dict[str, object]] = []
    service = watcher.PermissionWatcher(tmp_path / "state.json", posts.append)
    key = f"TCC:{_UV_PYTHON}"

    service.observe(
        _event(
            watcher.PermissionKind.TCC,
            watcher.EventPhase.PROMPTING,
            _T0,
            tool="/usr/bin/find",
        )
    )
    first_resolved_at = _T0 + timedelta(minutes=1)
    service.observe(
        _event(watcher.PermissionKind.TCC, watcher.EventPhase.RESOLVED, first_resolved_at)
    )
    caplog.clear()

    silent_started_at = first_resolved_at + timedelta(hours=11)
    service.observe(
        _event(
            watcher.PermissionKind.TCC,
            watcher.EventPhase.PROMPTING,
            silent_started_at,
            correlation_id="request-2",
            tool="/usr/bin/du",
        )
    )
    silent = service.pending[key]
    assert silent.mode == "silent"
    assert silent.alert_posted is False
    assert len(posts) == 2
    assert any(
        f"suppressing repeat alert for {key} (resolved {first_resolved_at.isoformat()})"
        in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    )

    silent_resolved_at = silent_started_at + timedelta(minutes=1)
    service.observe(
        _event(
            watcher.PermissionKind.TCC,
            watcher.EventPhase.RESOLVED,
            silent_resolved_at,
            correlation_id="request-2",
        )
    )
    assert service.pending == {}
    assert service.resolved[key] == silent_resolved_at
    assert len(posts) == 2

    next_started_at = silent_resolved_at + watcher.RECUR_SILENCE + timedelta(seconds=1)
    service.observe(
        _event(
            watcher.PermissionKind.TCC,
            watcher.EventPhase.PROMPTING,
            next_started_at,
            correlation_id="request-3",
            tool="/usr/bin/rg",
        )
    )
    assert service.pending[key].mode == "full"
    assert service.pending[key].alert_posted is True
    assert len(posts) == 3
    assert _only_alert(posts[-1])["startsAt"] == next_started_at.isoformat()


def test_failed_firing_post_retries_on_repeat_with_same_starts_at(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    attempts: list[dict[str, object]] = []

    def fail_once(payload: dict[str, object]) -> None:
        attempts.append(payload)
        if len(attempts) == 1:
            raise RuntimeError("gateway unavailable")

    caplog.set_level(logging.DEBUG, logger="ava.permission_watcher")
    service = watcher.PermissionWatcher(tmp_path / "state.json", fail_once)
    service.observe(_event(watcher.PermissionKind.TCC, watcher.EventPhase.PROMPTING, _T0))
    pending = next(iter(service.pending.values()))
    assert pending.alert_posted is False
    assert any(record.levelno == logging.WARNING for record in caplog.records)

    service.observe(
        _event(
            watcher.PermissionKind.TCC,
            watcher.EventPhase.PROMPTING,
            _T0 + timedelta(hours=6),
            correlation_id="request-2",
        )
    )
    assert len(attempts) == 2
    assert _only_alert(attempts[0])["startsAt"] == _T0.isoformat()
    assert _only_alert(attempts[1])["startsAt"] == _T0.isoformat()
    assert next(iter(service.pending.values())).alert_posted is True


def test_failed_resolved_post_still_closes_pending(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    calls = 0

    def fail_resolution(_payload: dict[str, object]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("gateway unavailable")

    caplog.set_level(logging.DEBUG, logger="ava.permission_watcher")
    service = watcher.PermissionWatcher(tmp_path / "state.json", fail_resolution)
    service.observe(_event(watcher.PermissionKind.TCC, watcher.EventPhase.PROMPTING, _T0))
    resolved_at = _T0 + timedelta(minutes=1)
    service.observe(_event(watcher.PermissionKind.TCC, watcher.EventPhase.RESOLVED, resolved_at))

    assert service.pending == {}
    assert service.resolved == {f"TCC:{_UV_PYTHON}": resolved_at}
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_state_round_trip_persists_modes_delivery_and_resolutions_and_loads_old_shape(
    tmp_path: Path,
) -> None:
    started_at = datetime.now(UTC) - timedelta(hours=2)
    state_path = tmp_path / "permission-watcher.json"
    posts: list[dict[str, object]] = []
    first = watcher.PermissionWatcher(state_path, posts.append)
    first.observe(
        _event(
            watcher.PermissionKind.TCC,
            watcher.EventPhase.PROMPTING,
            started_at,
            tool="/usr/bin/find",
        )
    )
    first.observe(
        _event(
            watcher.PermissionKind.TCC,
            watcher.EventPhase.RESOLVED,
            started_at + timedelta(minutes=1),
        )
    )
    first.observe(
        _event(
            watcher.PermissionKind.TCC,
            watcher.EventPhase.PROMPTING,
            started_at + timedelta(hours=1),
            correlation_id="request-2",
        )
    )
    first.observe(
        _event(
            watcher.PermissionKind.ALF,
            watcher.EventPhase.PROMPTING,
            started_at + timedelta(hours=1),
            subject="/Applications/Firewall.app",
        )
    )

    state = json.loads(state_path.read_text())
    silent_persisted = state["pending"][f"TCC:{_UV_PYTHON}"]
    full_persisted = state["pending"]["ALF:/Applications/Firewall.app"]
    assert "notified" not in silent_persisted
    assert "escalated" not in silent_persisted
    assert silent_persisted["mode"] == "silent"
    assert silent_persisted["alert_posted"] is False
    assert full_persisted["mode"] == "full"
    assert full_persisted["alert_posted"] is True
    assert state["resolved"] == {
        f"TCC:{_UV_PYTHON}": (started_at + timedelta(minutes=1)).isoformat()
    }

    restarted = watcher.PermissionWatcher(state_path, posts.append)
    assert restarted.pending[f"TCC:{_UV_PYTHON}"].mode == "silent"
    assert restarted.pending[f"TCC:{_UV_PYTHON}"].alert_posted is False
    assert restarted.pending["ALF:/Applications/Firewall.app"].mode == "full"
    assert restarted.pending["ALF:/Applications/Firewall.app"].alert_posted is True
    assert restarted.resolved == {f"TCC:{_UV_PYTHON}": started_at + timedelta(minutes=1)}

    legacy = state["pending"][f"TCC:{_UV_PYTHON}"]
    legacy.pop("mode")
    legacy.pop("alert_posted")
    legacy.pop("tool")
    state.pop("resolved")
    state_path.write_text(json.dumps(state))

    legacy_restarted = watcher.PermissionWatcher(state_path, posts.append)
    legacy_pending = legacy_restarted.pending[f"TCC:{_UV_PYTHON}"]
    assert legacy_pending.mode == "full"
    assert legacy_pending.alert_posted is False
    assert legacy_pending.tool is None
    assert legacy_restarted.resolved == {}


def test_state_save_prunes_resolutions_older_than_48_hours(tmp_path: Path) -> None:
    state_path = tmp_path / "permission-watcher.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "pending": {},
                "resolved": {
                    "TCC:/old": (_T0 - timedelta(hours=49)).isoformat(),
                    "TCC:/recent": (_T0 - timedelta(hours=47)).isoformat(),
                },
            }
        )
    )
    service = watcher.PermissionWatcher(state_path, lambda _payload: None)
    service.observe(
        _event(
            watcher.PermissionKind.ALF,
            watcher.EventPhase.PROMPTING,
            _T0,
            subject="/Applications/New.app",
        )
    )

    assert json.loads(state_path.read_text())["resolved"] == {
        "TCC:/recent": (_T0 - timedelta(hours=47)).isoformat()
    }


def test_state_load_drops_stale_pending_and_keeps_fresh_pending(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    stale_first_seen = now - timedelta(hours=30)
    fresh_first_seen = now - timedelta(hours=1)
    stale_key = "TCC:/usr/bin/find"
    fresh_key = f"TCC:{_UV_PYTHON}"
    state_path = tmp_path / "permission-watcher.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "pending": {
                    stale_key: {
                        "kind": "TCC",
                        "subject": "/usr/bin/find",
                        "first_seen": stale_first_seen.isoformat(),
                        "last_seen": stale_first_seen.isoformat(),
                        "correlation_id": None,
                    },
                    fresh_key: {
                        "kind": "TCC",
                        "subject": _UV_PYTHON,
                        "first_seen": fresh_first_seen.isoformat(),
                        "last_seen": fresh_first_seen.isoformat(),
                        "correlation_id": None,
                    },
                },
                "resolved": {},
            }
        )
    )
    caplog.set_level(logging.INFO, logger="ava.permission_watcher")
    posts: list[dict[str, object]] = []

    service = watcher.PermissionWatcher(state_path, posts.append)

    assert set(service.pending) == {fresh_key}
    assert [record.getMessage() for record in caplog.records if record.levelno == logging.INFO] == [
        f"dropping stale pending incident: {stale_key} (first_seen {stale_first_seen.isoformat()})"
    ]


def test_default_poster_reads_token_posts_to_loopback_and_retries_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from shared.config import settings

    env_path = tmp_path / ".env"
    env_path.write_text('AVA_OPS_ALERTS_WEBHOOK_TOKEN="secret-token"\n')
    calls: list[tuple[str, dict[str, object]]] = []
    sleeps: list[float] = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        calls.append((url, kwargs))
        if len(calls) == 1:
            return httpx.Response(503, request=httpx.Request("POST", url))
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(watcher.httpx, "post", fake_post)
    monkeypatch.setattr(watcher.time, "sleep", sleeps.append)
    monkeypatch.setattr(settings.gateway, "gateway_port", 8123)
    payload: dict[str, object] = {"source": "permission-watcher", "alerts": []}
    watcher.post_alert(payload, env_path=env_path)

    assert len(calls) == 2
    assert calls[0][0] == calls[1][0] == "http://127.0.0.1:8123/api/alerts"
    assert calls[1][1] == {
        "json": payload,
        "headers": {"X-Alerts-Token": "secret-token"},
        "timeout": 10.0,
    }
    assert sleeps == [2.0]


def test_default_poster_warns_and_raises_after_second_failure(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("AVA_OPS_ALERTS_WEBHOOK_TOKEN=secret-token\n")
    attempts = 0

    def fail_post(_url: str, **_kwargs: object) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("gateway unavailable")

    caplog.set_level(logging.WARNING, logger="ava.permission_watcher")
    monkeypatch.setattr(watcher.httpx, "post", fail_post)

    def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(watcher.time, "sleep", _no_sleep)
    with pytest.raises(httpx.ConnectError):
        watcher.post_alert({"source": "permission-watcher", "alerts": []}, env_path=env_path)

    assert attempts == 2
    assert any(
        "permission alert delivery failed after retry" in r.getMessage() for r in caplog.records
    )


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
