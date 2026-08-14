"""End-to-end: the agent DB-outage pause survives a real Postgres bounce.

Models the laptop-sleep / network-change case with a real `pg_ctl stop` then
`start` on the same port (not a mock): `_probe_db_reachable` sees the DB vanish
and come back, and `_wait_for_db_recovery` parks across the outage and returns
once the DB answers again — the "pause, then resume" the loop.py DB-outage branch
is built on. The branch ORCHESTRATION (reconcile + re-invoke) is unit-tested with
a faked probe in tests/agent/test_loop.py; this file pins the probe/wait against a
real bouncing server, the piece a mock cannot vouch for.

Runs its own throwaway Postgres on a fixed port + data dir so stop+restart reuses
the URL; never the session DB (stopping the shared one would break every test).
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from agent._runloop import _probe_db_reachable, _wait_for_db_recovery
from shared.config import settings
from shared.pg_tools import pg_tool


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _BounceablePg:
    """A throwaway Postgres the test can stop + restart on the same port/data dir
    (so the URL is stable across the bounce). `start` waits until it accepts
    connections; `stop` is an immediate shutdown (the abrupt loss a sleep causes).

    The data dir + unix-socket dir live under a SHORT `tempfile.mkdtemp` root, not
    pytest's deep `tmp_path`: the socket path `<dir>/.s.PGSQL.<port>` must stay
    under the OS 103-byte limit, and the nested pytest tmp path blows past it."""

    def __init__(self, root: Path, port: int) -> None:
        self._root = root
        self._data = root / "data"
        self._log = root / "pg.log"
        self.port = port
        self.url = f"postgresql://ava@127.0.0.1:{port}/postgres"

    def start(self) -> None:
        subprocess.run(  # noqa: S603 — argv is the resolved pg_ctl path + static flags
            [
                pg_tool("pg_ctl"),
                "-D",
                str(self._data),
                "-l",
                str(self._log),
                "-w",
                "-t",
                "30",
                "start",
                "-o",
                f"-p {self.port} -c listen_addresses=127.0.0.1 "
                f"-c unix_socket_directories={self._root} "
                "-c fsync=off -c full_page_writes=off -c synchronous_commit=off",
            ],
            check=True,
            capture_output=True,
        )

    def stop(self) -> None:
        subprocess.run(  # noqa: S603 — argv is the resolved pg_ctl path + static flags
            [pg_tool("pg_ctl"), "-D", str(self._data), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )


@pytest.fixture
def bounceable_pg() -> Iterator[_BounceablePg]:
    """initdb a throwaway cluster once (under a short temp root), start it, and
    yield a controller the test can stop/restart. Torn down on exit."""
    root = Path(tempfile.mkdtemp(prefix="ava-pgbounce-"))
    port = _free_port()
    subprocess.run(  # noqa: S603 — argv is the resolved initdb path + static flags
        [
            pg_tool("initdb"),
            "-D",
            str(root / "data"),
            "-U",
            "ava",
            "-A",
            "trust",
            "--no-sync",
            "--encoding=UTF8",
            "--locale=C",
        ],
        check=True,
        capture_output=True,
    )
    server = _BounceablePg(root, port)
    server.start()
    try:
        yield server
    finally:
        server.stop()
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.flaky  # real pg_ctl stop/start bounce + backoff timing (serial bucket)
async def test_probe_flips_across_real_pg_bounce(bounceable_pg: _BounceablePg) -> None:
    """The probe reads a real server going away and coming back: True while up,
    False the moment it is stopped (bounded by connect_timeout, not the OS TCP
    timeout), True again after it restarts on the same port."""
    assert await _probe_db_reachable(bounceable_pg.url) is True
    bounceable_pg.stop()
    assert await _probe_db_reachable(bounceable_pg.url) is False
    bounceable_pg.start()
    assert await _probe_db_reachable(bounceable_pg.url) is True


@pytest.mark.flaky  # real pg_ctl stop/start bounce + backoff timing (serial bucket)
async def test_wait_parks_then_resumes_after_pg_returns(
    bounceable_pg: _BounceablePg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_wait_for_db_recovery` stays parked while the DB is down and returns once
    it is back — the pause→resume the DB-outage branch relies on, against a real
    bounce. Point the loop's recovery dial at this server and shrink the backoff
    so the park/resume is observable within the test's time budget."""
    # _wait_for_db_recovery dials settings.data_plane.db_url — the one access URL,
    # and the suite runs with AVA_PGBOUNCER_ENABLED=false, so
    # patching db_url here is what the probe actually reads.
    monkeypatch.setattr(settings.data_plane, "db_url", bounceable_pg.url)
    monkeypatch.setattr("agent._runloop._DB_RECOVERY_BACKOFF_INITIAL_S", 0.05)
    monkeypatch.setattr("agent._runloop._DB_RECOVERY_BACKOFF_CAP_S", 0.05)

    bounceable_pg.stop()  # DB is down before the wait starts
    wait_task = asyncio.create_task(_wait_for_db_recovery(agent_id=7001))
    await asyncio.sleep(0.4)
    assert not wait_task.done(), "must stay parked while the DB is unreachable"

    bounceable_pg.start()  # DB comes back on the SAME url
    try:
        await asyncio.wait_for(wait_task, timeout=15.0)
    except TimeoutError:
        wait_task.cancel()
        pytest.fail("wait never returned after the DB came back — probe/backoff broken")
