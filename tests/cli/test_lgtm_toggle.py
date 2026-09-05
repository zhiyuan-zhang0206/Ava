"""`ava lgtm on|off|status` — the native observability-stack toggle.

Under test is the marker lifecycle (on writes it, off removes it BEFORE
stopping — else the gateway watchdog would resurrect local backends), script
wiring, and the native status view.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import cli.commands as commands_ns
from cli.commands import _lgtm, _lgtm_native


class _Result:
    returncode = 0


def _fail_on_docker_query(_name: str) -> None:
    pytest.fail("native lifecycle must not query the Docker CLI")


def _fake_backend_pids(_native_dir: Path) -> dict[str, str | None]:
    return {"loki": "101", "prometheus": None}


def _fail_run(*_args: object, **_kwargs: object) -> None:
    pytest.fail("native status must not run a container command")


def _wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, list[tuple[list[str], Path]]]:
    """Point marker + deploy dir at tmp, record subprocess invocations."""
    marker = tmp_path / "home" / "lgtm-host"
    marker.parent.mkdir(parents=True, exist_ok=True)
    deploy_dir = tmp_path / "repo" / "deploy" / "lgtm"
    deploy_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_lgtm, "lgtm_host_marker", lambda: marker)
    monkeypatch.setattr(_lgtm, "lgtm_deploy_dir", lambda _repo: deploy_dir)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(commands_ns, "_repo_root", lambda: tmp_path / "repo")

    def noop_native(_repo: Path, _home: Path) -> None:
        return None

    monkeypatch.setattr(_lgtm_native, "ensure_lgtm_native", noop_native)

    calls: list[tuple[list[str], Path]] = []

    def fake_run(cmd: list[str], **kw: object) -> _Result:
        calls.append((cmd, Path(str(kw["cwd"]))))
        return _Result()

    monkeypatch.setattr(_lgtm.subprocess, "run", fake_run)
    return marker, calls


def test_on_writes_marker_and_runs_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker, calls = _wire(monkeypatch, tmp_path)

    assert _lgtm.cmd_lgtm_on() == 0
    assert marker.exists()
    assert [c[0] for c in calls] == [["bash", "start.sh"]]
    assert calls[0][1].name == "lgtm"


def test_on_is_idempotent_with_existing_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker, calls = _wire(monkeypatch, tmp_path)
    marker.touch()

    assert _lgtm.cmd_lgtm_on() == 0
    assert marker.exists()
    assert [c[0] for c in calls] == [["bash", "start.sh"]]


def test_on_installs_native_backends_before_starting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker, _calls = _wire(monkeypatch, tmp_path)
    events: list[str] = []
    native_home = tmp_path / "home"

    def record_native(repo: Path, home: Path) -> None:
        events.append(f"native:{repo}:{home}")

    monkeypatch.setattr(
        _lgtm_native,
        "ensure_lgtm_native",
        record_native,
    )

    def fake_run(_cmd: list[str], **_kw: object) -> _Result:
        events.append("start")
        return _Result()

    monkeypatch.setattr(_lgtm.subprocess, "run", fake_run)
    monkeypatch.setattr("shared.paths.ava_home", lambda: native_home)

    assert _lgtm.cmd_lgtm_on() == 0
    assert marker.exists()
    assert events == [f"native:{tmp_path / 'repo'}:{native_home}", "start"]


def test_off_removes_marker_then_stops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The marker must be gone by the time stop.sh runs — with it still in
    place the gateway watchdog brings local backends back within a minute."""
    marker, calls = _wire(monkeypatch, tmp_path)
    marker.touch()
    monkeypatch.setattr(shutil, "which", _fail_on_docker_query)

    marker_present_at_stop: list[bool] = []

    def fake_run(cmd: list[str], **kw: object) -> _Result:
        marker_present_at_stop.append(marker.exists())
        calls.append((cmd, Path(str(kw["cwd"]))))
        return _Result()

    monkeypatch.setattr(_lgtm.subprocess, "run", fake_run)

    assert _lgtm.cmd_lgtm_off() == 0
    assert not marker.exists()
    assert [c[0] for c in calls] == [["bash", "stop.sh"]]
    assert marker_present_at_stop == [False]


def test_off_without_marker_still_stops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _marker, calls = _wire(monkeypatch, tmp_path)

    assert _lgtm.cmd_lgtm_off() == 0
    assert [c[0] for c in calls] == [["bash", "stop.sh"]]


def test_on_without_docker_starts_native_backends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker, calls = _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(shutil, "which", _fail_on_docker_query)

    assert _lgtm.cmd_lgtm_on() == 0
    assert marker.exists()
    assert [command for command, _cwd in calls] == [["bash", "start.sh"]]


def test_status_without_marker_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(_lgtm, "is_lgtm_host", lambda: False)

    assert _lgtm.cmd_lgtm_status() == 0
    assert "not the LGTM host" in capsys.readouterr().out


def test_status_reports_native_jobs_without_docker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The native PID helper is the status source; no container query remains."""
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(_lgtm, "is_lgtm_host", lambda: True)
    monkeypatch.setattr(_lgtm_native, "backend_pids", _fake_backend_pids)
    monkeypatch.setattr(
        _lgtm,
        "probe_statuses",
        lambda: [("loki", True), ("prometheus", True), ("grafana", False)],
    )
    monkeypatch.setattr(shutil, "which", _fail_on_docker_query)
    monkeypatch.setattr(_lgtm.subprocess, "run", _fail_run)

    assert _lgtm.cmd_lgtm_status() == 0
    output = capsys.readouterr().out
    assert "com.ava.loki      101" in output
    assert "com.ava.prometheus not-running" in output
    assert "✓ loki readiness" in output
    assert "✗ grafana readiness" in output
    assert "docker" not in output.lower()
