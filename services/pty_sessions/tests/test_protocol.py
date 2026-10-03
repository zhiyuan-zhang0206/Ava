"""The pty-sessions wire protocol and its ownership probe, against a real service.

Every request is one JSON line; every response echoes the request's `id`, which
is what the shared ownership probe (`services.healthchecks.owned_service`) pairs
a ping with. A bad request must answer, never wedge or kill the service.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import psutil
import pytest

from base.daemon.health import ProbeVerdict
from base.native_process.ownership import OwnedProcess
from base.sessions.pty import client, protocol
from base.sessions.pty.paths import service_socket_path
from services.healthchecks import owned_service
from tests.path_scoped import pty_shells as support
from tests.path_scoped.pty_service import PtyServiceProcess
from tests.path_scoped.pty_service import pty_service as pty_service

pytestmark = pytest.mark.usefixtures("pty_service")


def _raw(payload: bytes) -> dict[str, Any]:
    """Send raw bytes, read the one response line."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(str(service_socket_path()))
        conn.sendall(payload)
        line = protocol.read_line(conn)
    assert line is not None
    return protocol.decode_object(line)


def test_ping_echoes_the_request_id(pty_service: PtyServiceProcess) -> None:
    response = _raw(b'{"id": 4711, "method": "ping"}\n')
    assert response["id"] == 4711
    assert response["ok"] is True
    assert response["data"]["pid"] == pty_service.pid


def test_the_ownership_probe_reads_the_service_as_up(pty_service: PtyServiceProcess) -> None:
    """`probe_owned_service` pairs `{"id": 0, "method": "ping"}` with an `ok` answer
    and requires the connected peer to belong to the root-owned generation."""
    owner = OwnedProcess.capture(psutil.Process(pty_service.pid))
    probe = owned_service._owned_ping(lambda: owner, service_socket_path())
    assert probe.verdict is ProbeVerdict.ALIVE, probe.detail


def test_the_ownership_probe_refuses_a_peer_outside_the_generation(
    pty_service: PtyServiceProcess,
) -> None:
    del pty_service
    with subprocess.Popen(["sleep", "60"], start_new_session=True) as stranger:
        owner = OwnedProcess.capture(psutil.Process(stranger.pid))
        probe = owned_service._owned_ping(lambda: owner, service_socket_path())
        stranger.kill()
    assert probe.verdict is not ProbeVerdict.ALIVE


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        (b'{"id": 1, "method": "teleport"}\n', "unknown method"),
        (b'{"id": 2}\n', "unknown method"),
        (b"not json\n", "bad request"),
        (b"[1, 2]\n", "bad request"),
        (b'{"id": 3, "method": "has"}\n', "name must be"),
        (
            b'{"id": 4, "method": "new", "name": "Bad_Name", "cwd": "/tmp"}\n',
            "invalid session name",
        ),
        (
            b'{"id": 5, "method": "new", "name": "ok", "cwd": "/nonexistent-dir"}\n',
            "not a directory",
        ),
        (
            b'{"id": 6, "method": "new", "name": "ok", "cwd": "/tmp", "env": {"A=B": "x"}}\n',
            "cannot be forwarded",
        ),
        (
            b'{"id": 7, "method": "new", "name": "ok", "cwd": "/tmp", "env": {"A": 1}}\n',
            "object of strings",
        ),
    ],
)
def test_a_bad_request_is_answered_and_the_service_keeps_serving(
    payload: bytes, needle: str
) -> None:
    response = _raw(payload)
    assert response["ok"] is False
    assert needle in response["error"]
    assert client.request("ping")["pid"]


def test_an_oversize_request_is_cut_off_without_wedging_the_service() -> None:
    """The service stops reading at the limit and hangs up; the sender may see the
    refusal or only the reset, and the service keeps serving either way."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(str(service_socket_path()))
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            conn.sendall(b"x" * (protocol.MAX_REQUEST_BYTES + 70000))
    assert client.request("ping")["pid"]


def test_a_client_that_hangs_up_mid_request_does_not_hurt_the_service() -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(str(service_socket_path()))
        conn.sendall(b'{"id": 9, "meth')
    assert client.request("ping")["pid"]


def test_the_client_rejects_an_answer_to_another_request(
    pty_service: PtyServiceProcess,
) -> None:
    """A response whose id is not the request's is somebody else's answer."""
    pty_service.stop()
    path = service_socket_path()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as fake:
        fake.bind(str(path))
        fake.listen(1)

        def answer_wrongly() -> None:
            conn, _ = fake.accept()
            with conn:
                protocol.read_line(conn)
                conn.sendall(protocol.encode(protocol.ok("not-the-request-id")))

        responder = threading.Thread(target=answer_wrongly)
        responder.start()
        with pytest.raises(client.ServiceUnavailableError, match="answered request"):
            client.request("ping")
        responder.join(timeout=10)
    path.unlink(missing_ok=True)


def test_queries_read_a_missing_service_as_no_sessions(unit_home: Path) -> None:
    """With no service running (the fixture's is stopped), a query answers truthfully."""
    del unit_home
    assert client.has_session("ava-test-anything") is False
    assert client.list_sessions() == []


def test_a_mutating_request_without_a_service_raises(
    pty_service: PtyServiceProcess, unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pty_service.stop()
    monkeypatch.setattr(client, "CONNECT_WAIT_S", 0.2)
    with pytest.raises(client.ServiceUnavailableError):
        support.new("ava-test-nobody-home", unit_home)


def test_a_mutating_request_waits_for_a_service_that_is_still_starting(
    pty_service: PtyServiceProcess, unit_home: Path
) -> None:
    """A start races its own clients: `new` dials until the socket accepts."""
    pty_service.stop()
    starter = threading.Timer(0.5, pty_service.start)
    starter.start()
    try:
        assert support.new("ava-test-early-bird", unit_home) is True
    finally:
        starter.join()


def test_a_second_service_refuses_to_start_over_a_live_one(
    pty_service: PtyServiceProcess,
) -> None:
    from tests.path_scoped.pty_service import _REPO

    env = {"AVA_HOME": str(pty_service.home), "PATH": "/usr/bin:/bin", "AVA_CONFIG_FETCH": "skip"}
    result = subprocess.run(
        [sys.executable, "-m", "services.pty_sessions.daemon"],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert "another pty-sessions service holds" in result.stderr
    assert client.request("ping")["pid"] == pty_service.pid, "the live service was disturbed"


def test_the_request_encoding_round_trips_through_json() -> None:
    assert json.loads(protocol.encode(protocol.ok(3, {"a": 1}))) == {
        "id": 3,
        "ok": True,
        "code": 0,
        "data": {"a": 1},
        "error": None,
    }
