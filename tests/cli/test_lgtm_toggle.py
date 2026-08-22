"""`ava lgtm on|off|status` — the observability-stack toggle.

No docker: subprocess/shutil are monkeypatched; under test is the marker
lifecycle (on writes it, off removes it BEFORE stopping — else the gateway
watchdog would resurrect the containers) and the script wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import cli.commands as commands_ns
from cli.commands import _lgtm


class _Result:
    returncode = 0


def _wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, list[tuple[list[str], Path]]]:
    """Point marker + compose dir at tmp, record subprocess invocations."""
    marker = tmp_path / "home" / "lgtm-host"
    marker.parent.mkdir(parents=True, exist_ok=True)
    compose_dir = tmp_path / "repo" / "deploy" / "lgtm"
    compose_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_lgtm, "lgtm_host_marker", lambda: marker)
    monkeypatch.setattr(_lgtm, "lgtm_compose_dir", lambda _repo: compose_dir)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(commands_ns, "_repo_root", lambda: tmp_path / "repo")
    monkeypatch.setattr(_lgtm.shutil, "which", lambda _name: "/usr/local/bin/docker")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(_lgtm, "machine_role", lambda: frozenset({"gateway"}))
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
    assert calls[0][0][-2:] == ["bash", "start.sh"]
    assert calls[0][1].name == "lgtm"


def test_on_is_idempotent_with_existing_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker, calls = _wire(monkeypatch, tmp_path)
    marker.touch()

    assert _lgtm.cmd_lgtm_on() == 0
    assert marker.exists()
    assert calls[0][0][-2:] == ["bash", "start.sh"]


def test_off_removes_marker_then_stops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The marker must be gone by the time stop.sh runs — with it still in
    place the gateway watchdog brings the containers back within a minute."""
    marker, calls = _wire(monkeypatch, tmp_path)
    marker.touch()

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


def test_on_without_docker_fails_fast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker, calls = _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(_lgtm.shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    assert _lgtm.cmd_lgtm_on() == 1
    assert not marker.exists()
    assert calls == []


def test_on_pure_runner_fails_before_writing_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker, calls = _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(_lgtm, "machine_role", lambda: frozenset({"agent-runner"}))

    assert _lgtm.cmd_lgtm_on() == 1
    assert not marker.exists()
    assert calls == []


def test_status_without_marker_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(_lgtm, "is_lgtm_host", lambda: False)

    assert _lgtm.cmd_lgtm_status() == 0
    assert "not the LGTM host" in capsys.readouterr().out
