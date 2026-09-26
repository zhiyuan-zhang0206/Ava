"""Native endpoint ownership must precede a successful readiness claim."""

from __future__ import annotations

import contextlib
import socket
import subprocess
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path

import psutil
import pytest

from services.healthchecks import owned_service as probe
from shared.daemon_health import DaemonProbe
from shared.native_process.ownership import OwnedProcess


@contextlib.contextmanager
def unix_server() -> Generator[tuple[Path, OwnedProcess]]:
    if sys.platform not in {"darwin", "linux"}:
        pytest.skip("native Unix peer PID contract is supported on macOS and Linux")
    with tempfile.TemporaryDirectory(prefix="ava-peer-", dir="/tmp") as directory:
        path = Path(directory) / "socket"
        code = """
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(sys.argv[1]); s.listen()
print('ready', flush=True)
while True:
    c, _ = s.accept()
    with c:
        if c.recv(4096):
            c.sendall(b'{"id":0,"ok":true,"result":"pong"}\\n')
"""
        process = subprocess.Popen(  # noqa: S603 — test-owned Python and socket path
            [sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True
        )
        try:
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "ready"
            yield path, OwnedProcess.capture(psutil.Process(process.pid))
        finally:
            process.terminate()
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()


def test_unix_ping_uses_kernel_peer_pid() -> None:
    with unix_server() as (path, owner):
        assert probe._owned_ping(lambda: owner, path).alive


def test_unix_pong_from_another_generation_is_not_readiness() -> None:
    with unix_server() as (_other_path, other), unix_server() as (path, _owner):
        result = probe._owned_ping(lambda: other, path)
        assert result.verdict.value == "port-taken"


def test_tcp_owned_listener_requires_application_readiness() -> None:
    owner = OwnedProcess.capture(psutil.Process())
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert probe._owned_tcp(owner, port, lambda: True).alive
        result = probe._owned_tcp(owner, port, lambda: DaemonProbe.down("application unready"))
        assert result.verdict.value == "down"
        assert result.detail == "application unready"


def test_tcp_foreign_listener_never_runs_the_application_probe() -> None:
    with unix_server() as (_path, foreign), socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()

        def unexpected() -> bool:
            pytest.fail("protocol probe must not accept another generation's listener")

        assert probe._owned_tcp(foreign, listener.getsockname()[1], unexpected).terminal


def test_unobservable_root_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_service: str) -> None:
        raise probe.RootClientError("ownership socket unavailable")

    def _fake_listener_pids(_port: int) -> set[int]:
        return {123}

    monkeypatch.setattr(probe, "owned_process", unavailable)
    monkeypatch.setattr(probe, "listener_pids", _fake_listener_pids)
    assert probe.probe("milvus").verdict.value == "unavailable"


def test_first_start_does_not_require_a_root_before_an_endpoint_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(_service: str) -> None:
        pytest.fail("an absent endpoint does not need an already-running root")

    monkeypatch.setattr(probe, "owned_process", unexpected)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    result = probe.probe_endpoint("new-service", port, lambda: True)
    assert result.verdict.value == "down", result.detail
