"""Application responses determine availability without native ownership queries."""

from __future__ import annotations

import contextlib
import subprocess
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from base.daemon.health import DaemonProbe
from services.supervision.healthchecks import protocol_probe as probe


@contextlib.contextmanager
def unix_server(payload: bytes) -> Generator[Path]:
    if sys.platform == "win32":
        pytest.skip("Unix socket fixture")
    with tempfile.TemporaryDirectory(prefix="ava-ping-", dir="/tmp") as directory:
        path = Path(directory) / "socket"
        code = """
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(sys.argv[1]); s.listen()
print('ready', flush=True)
c, _ = s.accept()
with c:
    if c.recv(4096):
        c.sendall(bytes.fromhex(sys.argv[2]))
"""
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(path), payload.hex()],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "ready"
            yield path
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
            if child.stdout is not None:
                child.stdout.close()


def test_unix_pong_needs_no_root_or_native_inspection(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("read-only protocol health must not inspect native ownership")

    monkeypatch.setattr("psutil.process_iter", forbidden)
    monkeypatch.setattr("base.native_process.root_control.client.owned_process", forbidden)
    with unix_server(b'{"id":0,"ok":true}\n') as path:
        assert probe.ping(path).alive


@pytest.mark.parametrize("payload", [b'{"id":1,"ok":true}\n', b'{"id":0,"ok":false}\n', b""])
def test_unix_invalid_or_closed_ping_is_down(payload: bytes) -> None:
    with unix_server(payload) as path:
        assert not probe.ping(path).alive


@pytest.mark.parametrize("ready", [True, False])
def test_protocol_result_needs_no_process_census(
    ready: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("process census is not a protocol prerequisite")

    monkeypatch.setattr("psutil.process_iter", forbidden)
    assert probe.probe_protocol(lambda: ready).alive is ready


def test_protocol_specific_failure_detail_is_preserved() -> None:
    result = DaemonProbe.down("application unready")
    assert probe.probe_protocol(lambda: result) is result


def test_missing_unix_endpoint_is_down(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("base.paths.mcp_daemon_shared_socket", lambda: tmp_path / "missing")
    assert probe.probe("mcp-daemon").verdict.value == "down"
