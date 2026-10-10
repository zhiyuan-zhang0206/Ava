"""A Postgres fast shutdown blocked by a hung archive command is ended, not waited on forever.

The real Postgres runs with an archive command that never returns (`sleep`, a stand-in for
a wal-g stuck on a blackholed network). A fast shutdown cannot finish while the archiver
waits on it; the data-plane stop must end it with an immediate shutdown inside its budget,
kill the archive command it left behind, and say so, without failing the stop (a failure
would trigger the compensating `ava start`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import psutil
import psycopg
import pytest

from base.cluster import ownership
from base.cluster import postgres as pg
from base.native_process.ownership import capture_tree
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane import maintenance_stop as plane

HUNG_SECONDS = "3617"  # a marker only this test's archive command carries


@pytest.fixture(autouse=True)
def config_boot_environment() -> Generator[None]:
    """Restore process delivery from the maintenance operation's boot."""
    with patch.dict(os.environ):
        yield


def _hung_archive_args(**_inputs: object) -> list[str]:
    return [
        "-c",
        "archive_mode=on",
        "-c",
        f"archive_command=sleep {HUNG_SECONDS}",
        "-c",
        "archive_timeout=1s",
    ]


def _private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, int]:
    from base.config import settings
    from tests._containers import _free_port

    home = tmp_path / "home"
    home.mkdir()
    port = _free_port()
    monkeypatch.setenv("AVA_HOME", str(home))
    monkeypatch.setattr(settings.data_plane, "db_url", f"postgresql://test@127.0.0.1:{port}/test")
    monkeypatch.setattr(settings.data_plane, "redis_url", f"redis://127.0.0.1:{_free_port()}")
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", "")
    return home / "pg", port


def _hung_archive_commands(owner: pg.OwnedProcess) -> list[psutil.Process]:
    found: list[psutil.Process] = []
    for member in capture_tree(owner):
        process = psutil.Process(member.pid)
        if HUNG_SECONDS in " ".join(process.cmdline()) and process.name() == "sleep":
            found.append(process)
    return found


def _wait_for_hung_archive_command(data: Path) -> list[psutil.Process]:
    owner = ownership.postgres(data)
    assert owner is not None
    deadline = time.monotonic() + 30
    with psycopg.connect(instance.pg_admin_url(_port_of(data)), autocommit=True) as conn:
        while time.monotonic() < deadline:
            conn.execute("SELECT pg_logical_emit_message(true, 'x', 'y')")
            conn.execute("SELECT pg_switch_wal()")
            if found := _hung_archive_commands(owner):
                return found
            time.sleep(0.2)
    raise AssertionError("the archiver never started the archive command")


def _port_of(data: Path) -> int:
    receipt = pg._read(data)
    assert receipt is not None
    return receipt.port


@pytest.mark.skipif(sys.platform == "win32", reason="owned POSIX PostgreSQL")
def test_a_hung_archive_command_ends_in_an_immediate_shutdown_inside_the_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    data, port = _private_home(tmp_path, monkeypatch)
    monkeypatch.setattr(instance, "archive_pg_args", _hung_archive_args)
    # Small legs: the reserve is cleanup + kill + cleanup = 3 s, so a 7 s stop gives the
    # fast shutdown 4 s before it is ended.
    monkeypatch.setattr(plane, "PROCESS_CLEANUP_WAIT_S", 1.0)
    monkeypatch.setattr(plane, "PROCESS_KILL_WAIT_S", 1.0)
    events: list[tuple[str, dict[str, object]]] = []

    def emit(_stream: str, kind: str, **fields: object) -> None:
        events.append((kind, fields))

    monkeypatch.setattr("base.telemetry.emit", emit)
    notes: list[str] = []
    try:
        assert instance._start_pg(port, "", retained_children=retained_children) == 0
        owner = ownership.postgres(data)
        assert owner is not None
        hung = _wait_for_hung_archive_command(data)
        hung_pids = {process.pid for process in hung}

        started = time.monotonic()
        stopped = plane.stop(7, notes=notes, retained_children=retained_children)
        elapsed = time.monotonic() - started

        assert stopped == ["postgres"]
        assert elapsed < 7, "the stop completed inside its budget"
        assert not owner.live() and ownership.postgres(data) is None
        assert not [pid for pid in hung_pids if psutil.pid_exists(pid)], (
            "the archive command is gone"
        )
        (note,) = notes
        assert "fast shutdown did not complete" in note and "immediate shutdown" in note
        assert [kind for kind, _ in events] == ["postgres_stop_escalated"]
        assert events[0][1]["level"] == "error"
    finally:
        pg.stop(
            data, timeout=10, immediate_wait=1, kill_wait=1, retained_children=retained_children
        )
        for process in psutil.process_iter(["cmdline"]):
            if HUNG_SECONDS in " ".join(process.info["cmdline"] or []):
                process.kill()


def test_the_fast_shutdown_leaves_room_for_the_legs_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plane, "PROCESS_CLEANUP_WAIT_S", 10.0)
    monkeypatch.setattr(plane, "PROCESS_KILL_WAIT_S", 3.0)
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)

    assert plane._postgres_fast_budget(1300.0) == 277.0
    # a stop with less time than the reserve gives the fast shutdown all of it
    assert plane._postgres_fast_budget(1020.0) == 20.0
    # Exhaustion refuses admission instead of relying on a busy host to spend the budget.
    for deadline in (1000.0, 999.0):
        with pytest.raises(TimeoutError, match="stop deadline expired"):
            plane._postgres_fast_budget(deadline)


@pytest.fixture
def retained_children() -> list[subprocess.Popen[bytes]]:
    return []
