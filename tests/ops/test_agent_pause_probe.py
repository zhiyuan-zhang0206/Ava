"""`host_running()` / `ops_quiescent()`: the mode-aware owner recognition (W1.2e-2).

A root-driven host keeps its services as ava-root tree units, not session
records — the pidfile is the same, so the pidfile-only inconsistency check must
ask the root before it calls a live pid an unrecorded owner; the same holds for
`ops_quiescent()`'s "is ops even running" gate (task #3370).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
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


def _root_response(*, state: str, pid: int, unit_id: str = "agent-host") -> dict[str, object]:
    return {"ok": True, "result": {"units": [{"id": unit_id, "state": state, "pid": pid}]}}


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


def test_host_running_retries_the_scan_once_past_a_leaked_permission_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """macOS psutil can leak a raw PermissionError while building one process's
    info mid-iteration (sysctl raced with a process diverging under load); the
    identical read succeeds when re-probed, so the scan retries once and the
    real answer still lands (the 2026-09-18 flake)."""
    _stub_backend(monkeypatch, has_session=False)
    monkeypatch.setattr(settings.services, "agent_host_pidfile", tmp_path / "absent.pid")
    monkeypatch.setattr(agent_pause_probe, "ava_home", lambda: tmp_path)
    calls: list[int] = []

    class _UnrecordedHost:
        def __init__(self) -> None:
            self.info = {"pid": 4242, "cmdline": ["python", "-m", "services.agent_host.daemon"]}

        def environ(self) -> dict[str, str]:
            return {"AVA_HOME": str(tmp_path)}

    def process_iter(attrs: list[str]) -> Iterator[object]:
        del attrs
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("force permission denied (sysctl(KERN_PROCARGS2) -> errno 0)")
        return iter([_UnrecordedHost()])

    monkeypatch.setattr("psutil.process_iter", process_iter)
    with pytest.raises(RuntimeError, match="still running without its service record"):
        agent_pause_probe.host_running()
    assert len(calls) == 2


def test_host_running_stays_loud_when_the_process_scan_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A persistent scan failure must not silently read as "not running": the
    probe refuses with a typed error, so the pause path aborts instead of
    declaring the host absent."""
    _stub_backend(monkeypatch, has_session=False)
    monkeypatch.setattr(settings.services, "agent_host_pidfile", tmp_path / "absent.pid")
    monkeypatch.setattr(agent_pause_probe, "ava_home", lambda: tmp_path)
    calls: list[int] = []

    def process_iter(attrs: list[str]) -> Iterator[object]:
        del attrs
        calls.append(1)
        raise PermissionError("force permission denied")

    monkeypatch.setattr("psutil.process_iter", process_iter)
    with pytest.raises(RuntimeError, match="cannot verify whether an unrecorded agent-host"):
        agent_pause_probe.host_running()
    assert len(calls) == 2


# ─── ops_quiescent: the mode-aware "is ops running" gate (task #3370) ──────────


class _HealthzResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def read(self, _amount: int) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> _HealthzResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _HealthzOpener:
    """Records every opened URL; answers the one shared payload."""

    def __init__(self, calls: list[str], payload: dict[str, object]) -> None:
        self._calls = calls
        self._payload = payload

    def open(self, url: str, timeout: float | None = None) -> _HealthzResponse:
        del timeout
        self._calls.append(url)
        return _HealthzResponse(self._payload)


def _stub_ops_wait(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, calls: list[str]) -> None:
    """A quiescent ops healthz answer: this home, this pidfile, no admitted work."""
    pidfile = tmp_path / "ops.pid"
    pidfile.write_text(f"{os.getpid()}\n", encoding="utf-8")
    payload: dict[str, object] = {
        "home": str(tmp_path),
        "pid": os.getpid(),
        "maintenance": {"protocol": 1, "requests": 0, "workers": 0},
    }
    monkeypatch.setattr(agent_pause_probe, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(settings.services, "ops_pidfile", pidfile)
    monkeypatch.setattr(
        agent_pause_probe,
        "build_opener",
        lambda *_args, **_kwargs: _HealthzOpener(calls, payload),  # pyright: ignore[reportUnknownArgumentType]
    )


def test_ops_quiescent_skips_without_session_and_root_unit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Neither management mode claims ops is running: nothing to wait for."""
    _stub_backend(monkeypatch, has_session=False)
    _stub_root_client(monkeypatch, response={"ok": True, "result": {"units": []}})
    calls: list[str] = []
    _stub_ops_wait(monkeypatch, tmp_path, calls=calls)
    agent_pause_probe.ops_quiescent(0.5)
    assert calls == []


def test_ops_quiescent_skips_when_root_unit_is_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_backend(monkeypatch, has_session=False)
    _stub_root_client(
        monkeypatch, response=_root_response(state="stopped", pid=os.getpid(), unit_id="ops")
    )
    calls: list[str] = []
    _stub_ops_wait(monkeypatch, tmp_path, calls=calls)
    agent_pause_probe.ops_quiescent(0.5)
    assert calls == []


def test_ops_quiescent_skips_when_root_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_backend(monkeypatch, has_session=False)
    _stub_root_client(monkeypatch, unreachable=True)
    calls: list[str] = []
    _stub_ops_wait(monkeypatch, tmp_path, calls=calls)
    agent_pause_probe.ops_quiescent(0.5)
    assert calls == []


def test_ops_quiescent_waits_when_the_root_runs_ops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The W1.2e-2 gap: no session record, but the tree runs ops — the wait must run."""
    _stub_backend(monkeypatch, has_session=False)
    _stub_root_client(
        monkeypatch, response=_root_response(state="running", pid=os.getpid(), unit_id="ops")
    )
    calls: list[str] = []
    _stub_ops_wait(monkeypatch, tmp_path, calls=calls)
    agent_pause_probe.ops_quiescent(1.0)
    assert len(calls) == 1
    assert calls[0].endswith("/healthz")


def test_ops_quiescent_session_gate_does_not_consult_the_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Session first: a recorded `ava-ops` opens the wait without a root roundtrip."""
    _stub_backend(monkeypatch, has_session=True)

    def _explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("root consulted although the session records ops")

    monkeypatch.setattr("services.ava_root.client.RootClient", _explode)
    calls: list[str] = []
    _stub_ops_wait(monkeypatch, tmp_path, calls=calls)
    agent_pause_probe.ops_quiescent(1.0)
    assert len(calls) == 1
