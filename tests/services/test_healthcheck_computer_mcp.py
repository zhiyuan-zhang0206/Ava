"""Unit tests for the computer-mcp healthcheck probe.

The probe is a lock-free ping over the daemon's Unix socket; these tests run a
real in-process daemon on a short socket path and verify alive/dead verdicts.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

import services.healthchecks.computer_mcp as hc
from services.computer.mcp_daemon import ComputerMcpDaemon


@pytest.fixture
async def daemon_sock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A live ComputerMcpDaemon on a short /tmp socket; monkeypatch the
    healthcheck's socket path to it."""
    sock = f"/tmp/computer-hc-test-{os.getpid()}.sock"  # noqa: S108 — test-only short AF_UNIX path
    with suppress(OSError):
        Path(sock).unlink()
    daemon = ComputerMcpDaemon(sock=sock)
    server = await asyncio.start_unix_server(daemon.handle, path=sock)

    def _sock() -> Any:
        return sock

    monkeypatch.setattr(hc, "computer_mcp_socket", _sock)
    try:
        yield sock
    finally:
        server.close()
        await server.wait_closed()
        with suppress(OSError):
            Path(sock).unlink()


async def test_probe_alive(daemon_sock) -> None:
    # The blocking probe would deadlock the loop the fixture's server runs
    # on; bounce it to a worker thread.
    assert await asyncio.to_thread(hc._is_alive) is True


def test_probe_dead_when_socket_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "computer_mcp_socket", lambda: "/tmp/does-not-exist.sock")  # noqa: S108
    assert hc._is_alive() is False
