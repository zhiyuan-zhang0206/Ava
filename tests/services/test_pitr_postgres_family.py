"""Proven closure covers PostgreSQL's own children, which setsid() out of the group.

Every postmaster child (checkpointer, walwriter, each backend) leaves the
operation worker's process group at birth. The controller's closure, and a
later retirement, must still prove each recorded birth dead and no process
working inside a receipted data directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from services.pitr import operation_custody as custody
from services.pitr import worker_process as workers
from shared.native_process import native_boot_id
from shared.native_process.ownership import OwnedProcess
from shared.pg_tools import pg_tool
from tests.services.test_pitr_native_custody import _control_dir, _exited_worker, _kind
from tests.services.test_pitr_operation_owner import _release_held, _until, _worker

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="PITR is POSIX-only")
__all__ = ["_release_held"]  # the per-test release of unresolved leaders

_BUSY = "SELECT count(*) FROM generate_series(1, 20000000000)"
_POSTGRES_WORKER = """\
import psycopg
from services.pitr.worker_process import worker_request
from shared.native_process.ownership import OwnedProcess
from shared.pg_foreground import start_foreground_postgres, wait_foreground_postgres
import psutil
request, output = worker_request(sys.argv)
data, port = Path(request['data']), request['port']
pm = start_foreground_postgres(
    [request['postgres'], '-D', str(data), '-p', str(port), '-k', request['sock'],
     '-c', 'listen_addresses=127.0.0.1', '-c', 'fsync=off'],
    log=Path(request['log']))
wait_foreground_postgres(pm, log=Path(request['log']), port=port, data=data)
url = f'postgresql://ava@127.0.0.1:{port}/postgres'
subprocess.Popen([sys.executable, '-c',
    'import psycopg,sys; psycopg.connect(sys.argv[1]).execute(sys.argv[2])', url, __BUSY__])
with psycopg.connect(url, autocommit=True) as probe:
    while not probe.execute(
        "SELECT 1 FROM pg_stat_activity WHERE state = 'active' AND query LIKE 'SELECT count%'"
    ).fetchone():
        time.sleep(0.05)
family = [OwnedProcess.capture(child) for child in psutil.Process(pm.pid).children(recursive=True)]
staged = Path(request['state'] + '.tmp')
staged.write_text(json.dumps(dict(
    group=os.getpgrp(),
    family=[[m.pid, m.birth, m.starttime] for m in family],
    groups=[os.getpgid(m.pid) for m in family])))
staged.rename(request['state'])
if request['mode'] == 'exit':
    Path(sys.argv[2]).write_text('{}')
    sys.exit(0)
time.sleep(600)
""".replace("__BUSY__", repr(_BUSY))


@pytest.fixture(scope="module")
def pgdata() -> Iterator[Path]:
    """One initialized cluster per module, under a short socket-safe path."""
    with tempfile.TemporaryDirectory(prefix="pgfam-", dir="/tmp") as root:
        data = Path(root) / "data"
        subprocess.run(  # noqa: S603 -- disposable test cluster
            [
                str(pg_tool("initdb")),
                "-D",
                str(data),
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
        yield data


def _free_port() -> int:
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _family(state: Path) -> tuple[list[OwnedProcess], int, list[int]]:
    recorded = json.loads(state.read_text())
    members = [OwnedProcess(pid, birth, ticks) for pid, birth, ticks in recorded["family"]]
    return members, recorded["group"], recorded["groups"]


def _kill(members: list[OwnedProcess]) -> None:
    for member in members:
        with contextlib.suppress(Exception):
            member.send_signal(signal.SIGKILL)


@pytest.mark.parametrize("mode", ["cancel", "exit"])
async def test_closure_covers_postgres_children_outside_the_worker_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pgdata: Path, mode: str
) -> None:
    """A busy backend outlived the old "proven" group closure by its whole
    statement; now the postmaster is stopped cleanly and every recorded birth
    is dead before closure is recorded, on both the stop and the exit path."""
    state = tmp_path / "state.json"
    _worker(tmp_path, monkeypatch, _POSTGRES_WORKER)
    request = {
        "data": str(pgdata),
        "port": _free_port(),
        "sock": str(pgdata.parent),
        "postgres": str(pg_tool("postgres")),
        "log": str(tmp_path / "pg.log"),
        "state": str(state),
        "mode": mode,
    }
    task = asyncio.create_task(
        workers.run_operation("unused", request, kind=_kind(tmp_path), env=dict(os.environ))
    )
    members: list[OwnedProcess] = []
    try:
        await _until(state)
        members, group, groups = _family(state)
        assert members and group not in groups  # every child left the worker group
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 60)
        else:
            completed = await asyncio.wait_for(task, 60)
            await completed.commit(lambda: None)
        assert [member.pid for member in members if member.live()] == []
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        _kill(members)


def _holder(directory: Path) -> subprocess.Popen[bytes]:
    """A process working inside a data directory that no receipt recorded."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=directory)


def test_retirement_applies_the_postgres_family_proof(tmp_path: Path) -> None:
    """An empty worker group is not enough while a recorded postgres birth, or
    any process inside a receipted data directory, still runs."""
    data = tmp_path / "pgdata"
    data.mkdir()
    work = _control_dir(tmp_path, "orphaned-backend", worker=_exited_worker())
    backend = _holder(data)
    try:
        member = OwnedProcess.capture(psutil.Process(backend.pid))
        (work / "postmaster-x.json").write_text(
            json.dumps(
                {"pgdata": str(data.resolve()), "boot_id": native_boot_id(), "postmaster": None}
            )
        )
        (work / "family.json").write_text(
            json.dumps(
                {
                    "members": [
                        {"pid": member.pid, "birth": member.birth, "starttime": member.starttime}
                    ]
                }
            )
        )
        (report,) = custody.retire_blocked(_kind(tmp_path), confirm=True)
        assert not report.proven and str(backend.pid) in report.reason and work.is_dir()
    finally:
        backend.kill()
        backend.wait(timeout=10)
    (report,) = custody.retire_blocked(_kind(tmp_path), confirm=True)
    assert report.proven and report.entry is not None


_FAKE_POSTMASTER = """\
from services.pitr.worker_process import worker_request
from shared.pg_foreground import start_foreground_postgres
request, output = worker_request(sys.argv)
start_foreground_postgres(
    [sys.executable, '-c', 'import time; time.sleep(60)', '-D', request['data']],
    log=Path(request['log']))
Path(sys.argv[2]).write_text('{}')
"""


async def test_an_unprovable_postgres_family_blocks_until_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process the controller cannot attribute keeps working inside the data
    directory: closure is unresolved, the kind blocks, and retirement refuses
    until that process is gone."""
    data = tmp_path / "pgdata"
    data.mkdir()
    monkeypatch.setattr(workers, "CLOSE_DEADLINE_S", 1.0)
    _worker(tmp_path, monkeypatch, _FAKE_POSTMASTER)
    request = {"data": str(data), "log": str(tmp_path / "pg.log")}
    holder = _holder(data)
    try:
        with pytest.raises(custody.OperationCustodyError):
            await workers.run_operation("unused", request, kind=_kind(tmp_path), env={})
        (work,) = (tmp_path / "controls").glob(".operation-*")
        assert (work / "unresolved.json").is_file() and not (work / "closure.json").exists()
        (report,) = custody.retire_blocked(_kind(tmp_path), confirm=True)
        assert not report.proven
    finally:
        holder.kill()
        holder.wait(timeout=10)
    # The holder is gone: the next admission's retry proves the family closed.
    with pytest.raises(custody.OperationBlockedError, match="confirmed it later"):
        await workers.run_operation("unused", request, kind=_kind(tmp_path), env={})
    (report,) = custody.retire_blocked(_kind(tmp_path), confirm=True)
    assert report.proven and report.entry is not None and custody.held_operations() == []


def test_restore_sandboxes_are_receipted_for_their_controller(tmp_path: Path) -> None:
    """Restore proofs and operator drills start their sandbox through the
    receipted launch, so their controller can close its family."""
    from services.pitr.restore_postgres import _spawn_sandbox_postgres
    from shared import pg_foreground

    postgres = tmp_path / "postgres"
    postgres.write_text("#!/bin/sh\nexec sleep 30\n")
    postgres.chmod(0o700)
    controls, data = tmp_path / "controls", tmp_path / "data"
    controls.mkdir()
    data.mkdir()
    pg_foreground.record_postmasters_in(controls)
    try:
        process = _spawn_sandbox_postgres(postgres, data, tmp_path / "config", tmp_path / "log")
    finally:
        pg_foreground.record_postmasters_in(None)
    try:
        (receipt,) = pg_foreground.read_postmaster_receipts(controls)
        assert receipt.pgdata == data.resolve() and receipt.postmaster is not None
        assert receipt.postmaster.pid == process.pid and receipt.postmaster.live()
    finally:
        process.kill()
        process.wait(timeout=10)
