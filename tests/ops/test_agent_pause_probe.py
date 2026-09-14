"""`host_running()`: the recorded/root-owned owner recognition (W1.2e-2).

A root-driven host keeps its agent-host as an ava-root tree unit, not a session
record — the pidfile is the same, so the pidfile-only inconsistency check must
ask the root before it calls a live pid an unrecorded owner.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ops import agent_pause_probe
from shared.config import settings


def _pidfile(tmp_path: Path, pid: int) -> Path:
    path = tmp_path / "agent-host.pid"
    path.write_text(f"{pid}\n", encoding="utf-8")
    return path


def _stub_backend(monkeypatch: pytest.MonkeyPatch, *, has_session: bool) -> None:
    class _Backend:
        def has_session(self, name: str) -> bool:
            del name
            return has_session

    monkeypatch.setattr("shared.session_backend.get_backend", _Backend)


def _stub_root_client(
    monkeypatch: pytest.MonkeyPatch, *, response: object = None, unreachable: bool = False
) -> None:
    from services.ava_root.client import RootClientError

    class _Client:
        def __init__(self, socket_path: Path, *, timeout: float = 1.0) -> None:
            del socket_path, timeout

        def status(self) -> object:
            if unreachable:
                raise RootClientError("no root answers")
            return response

    monkeypatch.setattr("services.ava_root.client.RootClient", _Client)


def _root_response(*, state: str, pid: int) -> dict[str, object]:
    return {"ok": True, "result": {"units": [{"id": "agent-host", "state": state, "pid": pid}]}}


def test_host_running_true_when_session_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_backend(monkeypatch, has_session=True)
    assert agent_pause_probe.host_running() is True


def test_host_running_true_for_root_supervised_pidfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pid = os.getpid()
    monkeypatch.setattr(settings.services, "agent_host_pidfile", _pidfile(tmp_path, pid))
    _stub_backend(monkeypatch, has_session=False)
    _stub_root_client(monkeypatch, response=_root_response(state="running", pid=pid))
    assert agent_pause_probe.host_running() is True


def test_host_running_rejects_live_pid_when_root_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings.services, "agent_host_pidfile", _pidfile(tmp_path, os.getpid()))
    _stub_backend(monkeypatch, has_session=False)
    _stub_root_client(monkeypatch, unreachable=True)
    with pytest.raises(RuntimeError, match="without its owned service session"):
        agent_pause_probe.host_running()


def test_host_running_rejects_live_pid_when_root_does_not_own_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pid = os.getpid()
    monkeypatch.setattr(settings.services, "agent_host_pidfile", _pidfile(tmp_path, pid))
    _stub_backend(monkeypatch, has_session=False)
    _stub_root_client(monkeypatch, response=_root_response(state="running", pid=pid + 1))
    with pytest.raises(RuntimeError, match="without its owned service session"):
        agent_pause_probe.host_running()


def test_host_running_rejects_live_pid_when_root_unit_is_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pid = os.getpid()
    monkeypatch.setattr(settings.services, "agent_host_pidfile", _pidfile(tmp_path, pid))
    _stub_backend(monkeypatch, has_session=False)
    _stub_root_client(monkeypatch, response=_root_response(state="stopped", pid=pid))
    with pytest.raises(RuntimeError, match="without its owned service session"):
        agent_pause_probe.host_running()
