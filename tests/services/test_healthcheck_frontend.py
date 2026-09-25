"""Frontend readiness uses the root owner and rejects unrelated listeners."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from services.healthchecks import frontend as hc


class _FakeResult:
    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr


def _owner_of_this_process():
    """A real OwnedProcess for the pytest process — the lineage check runs for real."""
    import psutil

    from shared.proc_tree import OwnedProcess

    return OwnedProcess.capture(psutil.Process(os.getpid()))


def test_is_alive_curl_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args, **kwargs):
        assert args[0] == "curl"
        return _FakeResult(returncode=0)

    monkeypatch.setattr(hc.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_expected_owner", _owner_of_this_process)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_listener_pids", lambda _port: {os.getpid()})  # pyright: ignore[reportUnknownArgumentType]
    assert hc.probe_frontend().alive is True


def test_is_alive_rejects_200_outside_the_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    # issue #2123: an old orphan answering 200 on the app port is not frontend
    # health — the answering listener must belong to the expected owner.
    def fake_run(args, **kwargs):
        return _FakeResult(returncode=0)

    monkeypatch.setattr(hc.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_expected_owner", _owner_of_this_process)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_listener_pids", lambda _port: {os.getpid() + 99999})  # pyright: ignore[reportUnknownArgumentType]
    assert hc.probe_frontend().alive is False


def test_is_alive_curl_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args, **kwargs):
        return _FakeResult(returncode=7)  # curl exit 7 = "Failed to connect"

    monkeypatch.setattr(hc, "_expected_owner", _owner_of_this_process)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_listener_pids", lambda _port: {os.getpid()})

    monkeypatch.setattr(hc.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    assert hc.probe_frontend().alive is False


def test_is_alive_curl_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args, **kwargs):
        raise FileNotFoundError("curl not in PATH")

    monkeypatch.setattr(hc.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_expected_owner", _owner_of_this_process)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_listener_pids", lambda _port: {os.getpid()})
    assert hc.probe_frontend().alive is False


def test_is_alive_curl_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="curl", timeout=5)

    monkeypatch.setattr(hc.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_expected_owner", _owner_of_this_process)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_listener_pids", lambda _port: {os.getpid()})
    assert hc.probe_frontend().alive is False


# ─── the identity source, per the unit's management mode (task #3370) ──────────


def _root_response(*, state: str, pid: int) -> dict[str, object]:
    owner = _owner_of_this_process()
    return {
        "ok": True,
        "result": {
            "root": {"pid": owner.pid, "create_time": owner.birth, "starttime": owner.starttime},
            "units": [
                {
                    "id": "frontend",
                    "state": state,
                    "pid": pid,
                    "create_time": owner.birth,
                    "starttime": owner.starttime,
                }
            ],
        },
    }


def _stub_root_client(
    monkeypatch: pytest.MonkeyPatch, *, response: object = None, unreachable: bool = False
) -> None:
    from services.ava_root.client import RootClientError

    class _Client:
        def __init__(self, _socket_path: Path, *, timeout: float = 1.0) -> None:
            del timeout

        def status(self) -> object:
            if unreachable:
                raise RootClientError("no root answers")
            return response

    monkeypatch.setattr("services.ava_root.client.RootClient", _Client)


def test_expected_owner_reads_the_tree_when_root_driven(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_root_client(monkeypatch, response=_root_response(state="running", pid=os.getpid()))
    owner = hc._expected_owner()
    assert owner is not None
    assert owner.pid == os.getpid()


def test_expected_owner_ignores_the_session_record_when_root_driven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unit's own management mode is authoritative — no cross-reading."""
    from shared.session_record import SessionRecord

    _stub_root_client(monkeypatch, response=_root_response(state="running", pid=os.getpid()))

    def _explode(_path: Path) -> None:
        raise AssertionError("session record consulted in root mode")

    monkeypatch.setattr(SessionRecord, "read", _explode)
    assert hc._expected_owner() is not None


def test_expected_owner_is_none_when_the_tree_unit_is_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_root_client(monkeypatch, response=_root_response(state="stopped", pid=os.getpid()))
    assert hc._expected_owner() is None


def test_live_endpoint_is_unavailable_when_no_root_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_root_client(monkeypatch, unreachable=True)
    monkeypatch.setattr(hc, "_listener_pids", lambda _port: {os.getpid()})
    assert hc.probe_frontend().verdict.value == "unavailable"


def test_expected_owner_rejects_reused_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _owner_of_this_process()
    response = {
        "ok": True,
        "result": {
            "root": {"pid": owner.pid, "create_time": owner.birth, "starttime": owner.starttime},
            "units": [
                {
                    "id": "frontend",
                    "state": "running",
                    "pid": os.getpid(),
                    "create_time": 1.0,
                    "starttime": None,
                }
            ],
        },
    }
    _stub_root_client(monkeypatch, response=response)
    assert hc._expected_owner() is None


def test_owned_listener_with_failed_http_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_root_client(monkeypatch, response=_root_response(state="running", pid=os.getpid()))
    monkeypatch.setattr(hc, "_listener_pids", lambda _port: {os.getpid()})
    monkeypatch.setattr(hc, "_http_ok", lambda: False)
    probe = hc.probe_frontend()
    assert probe.verdict.value == "down"
    assert "not ready" in probe.detail


def test_probe_frontend_is_alive_under_the_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """The W1.2e-2 gap this closes: no session record, the unit owns the listener."""

    def fake_run(args, **kwargs):
        return _FakeResult(returncode=0)

    _stub_root_client(monkeypatch, response=_root_response(state="running", pid=os.getpid()))
    monkeypatch.setattr(hc.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_listener_pids", lambda _port: {os.getpid()})  # pyright: ignore[reportUnknownArgumentType]
    probe = hc.probe_frontend()
    assert probe.alive is True


def test_probe_frontend_port_taken_when_the_listener_is_outside_the_tree_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(args, **kwargs):
        return _FakeResult(returncode=0)

    _stub_root_client(monkeypatch, response=_root_response(state="running", pid=os.getpid()))
    monkeypatch.setattr(hc.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_listener_pids", lambda _port: {os.getpid() + 99999})  # pyright: ignore[reportUnknownArgumentType]
    probe = hc.probe_frontend()
    assert probe.verdict.value == "port-taken"


def test_probe_frontend_down_when_no_listener_and_no_tree_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_root_client(monkeypatch, response=_root_response(state="stopped", pid=os.getpid()))
    monkeypatch.setattr(hc, "_listener_pids", lambda _port: set())  # pyright: ignore[reportUnknownArgumentType]
    probe = hc.probe_frontend()
    assert probe.verdict.value == "down"
