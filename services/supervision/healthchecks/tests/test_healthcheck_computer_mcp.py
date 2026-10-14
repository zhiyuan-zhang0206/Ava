"""Unit tests for the computer-mcp healthcheck probe.

The probe is a lock-free ping over the daemon's Unix socket; these tests run a
real in-process daemon on a short socket path and verify alive/dead verdicts.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

import services.supervision.healthchecks.computer_mcp as hc
from base.db import Database
from base.host.permissions_helper.client import PermissionsHelperClient
from base.paths import computer_mcp_socket
from services.desktop.computer.mcp_daemon import ComputerMcpDaemon
from services.desktop.computer.tests.slices import computer_use_config


@pytest.fixture
async def daemon_home(database: Database):
    """A live ComputerMcpDaemon on the socket of a short /tmp home (AF_UNIX paths are about 100
    bytes at most; pytest's tmp_path can exceed that), and that home."""
    with tempfile.TemporaryDirectory(dir="/tmp") as name:
        home = Path(name)
        sock = computer_mcp_socket(home)
        sock.parent.mkdir(parents=True)
        daemon = ComputerMcpDaemon(
            computer_use_config(),
            database,
            sock=str(sock),
            helper=PermissionsHelperClient("/unused.sock"),
        )
        server = await asyncio.start_unix_server(daemon.handle, path=str(sock))
        try:
            yield home
        finally:
            server.close()
            await server.wait_closed()


async def test_probe_alive(daemon_home: Path) -> None:
    # The blocking probe would deadlock the loop the fixture's server runs
    # on; bounce it to a worker thread.
    assert await asyncio.to_thread(hc.is_alive, daemon_home) is True


def test_probe_dead_when_socket_gone(tmp_path: Path) -> None:
    assert hc.is_alive(tmp_path) is False
