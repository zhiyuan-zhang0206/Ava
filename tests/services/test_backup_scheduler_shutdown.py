"""Real signal regressions: stopping a scheduler must stop its blocking job.

The sweep's bounded-exit regression (task #4224) rides the shared child-process
harness: production ``main()`` must exit within a small bound of SIGTERM even
with a default-executor job mid-flight.
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
import time
from collections.abc import Iterator
from datetime import datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import psutil
import pytest

from ops.agent_pause import PAUSE_TIMEOUT_SECONDS
from services.backup_scheduler import daemon
from services.backup_scheduler.worker import run_job
from tests.services.daemon_shutdown_test_support import (
    EXIT_BOUND_S,
    KILL_SLACK_S,
    spawn_child,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="backup scheduler is POSIX-only")


@pytest.fixture
def postgres_base(tmp_path: Path) -> Iterator[Path]:
    # Keep the Unix socket path below macOS's AF_UNIX limit. The parent owns
    # cleanup because SIGKILL can bypass a child process's context managers.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="bkpg-") as base:
        yield Path(base)
    pgdata = tmp_path / "pgdata"
    if pgdata.exists():
        assert not Path(pgdata.read_text()).exists()


def _block(root: Path, mode: str) -> None:
    child_code = (
        "import os, signal, time; from pathlib import Path; "
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if mode == "stubborn" else "")
        + f"Path({str(root / 'child')!r}).write_text(str(os.getpid())); time.sleep(600)"
    )
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / "worker").write_text(str(os.getpid()))
    subprocess.run(  # noqa: S603 -- fixed disposable test child
        [sys.executable, "-c", child_code], check=True, timeout=600
    )


def _backup(root: Path, mode: str, now: datetime) -> None:
    from services import backup
    from services.pitr import store_factory

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        size_path = kwargs["size_path"]
        assert isinstance(size_path, Path)
        size_path.write_bytes(b"completed stage")
        if kwargs["label"] == mode or mode == "stubborn":
            _block(root, mode)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def key(directory: Path) -> Path:
        path = directory / "test.key"
        path.write_bytes(b"private-key")
        return path

    store = SimpleNamespace(put_base_if_absent=lambda **_kwargs: _block(root, mode))
    with (
        patch.object(backup, "backup_dir", return_value=root / "artifacts"),
        patch.object(backup, "pg_tool", return_value=Path("pg_dump")),
        patch.object(backup, "_db_size_breakdown", return_value="test"),
        patch.object(backup, "_run_with_progress", run),
        patch.object(backup, "_key_file", key),
        patch.object(
            store_factory,
            "get_store_group",
            return_value=SimpleNamespace(restartable_streaming_object_store=lambda: store),
        ),
    ):
        backup.run_backup(
            now, db_url="postgresql://ava@127.0.0.1:1/test", publish=mode == "publish"
        )


def _restore(root: Path, mode: str, postgres_base: Path) -> None:
    from shared.pg_tools import throwaway_postgres

    with throwaway_postgres(base=postgres_base, foreground=True) as url:
        import psycopg

        with psycopg.connect(url) as conn:
            row = conn.execute("SHOW data_directory").fetchone()
            assert row is not None
            data = Path(row[0])
            (root / "postgres").write_text(
                data.joinpath("postmaster.pid").read_text().splitlines()[0]
            )
            (root / "pgdata").write_text(str(data))
        _block(root, mode)


def _exercise_daemon(root: Path, mode: str, postgres_base: Path) -> None:
    state = daemon._BackupState()
    pidfile = root / "daemon.pid"
    restore_mode = mode.startswith("restore")
    job = partial(_restore, root, "stubborn" if mode == "restore-stubborn" else mode, postgres_base)

    def record_success(_now: datetime) -> None:
        (root / "restore-success").touch()

    async def loop(_state: object) -> None:
        if restore_mode:
            await daemon._run_due_local_dump_restore(datetime.now().astimezone())
        else:
            await daemon._backup_loop(state)

    with (
        patch.object(daemon, "_is_running", return_value=False),
        patch.object(daemon, "_write_pidfile", lambda: pidfile.write_text(str(os.getpid()))),
        patch.object(daemon, "_remove_pidfile", lambda: pidfile.unlink(missing_ok=True)),
        patch.object(daemon, "start_health_server", AsyncMock(return_value=object())),
        patch.object(daemon, "stop_health_server", AsyncMock()),
        patch.object(daemon, "is_due", return_value=True),
        patch.object(daemon, "run_backup", partial(_backup, root, mode)),
        patch.object(daemon, "run_local_dump_restore", job),
        patch.object(daemon, "load_local_dump_restore_success", return_value=None),
        patch.object(daemon, "local_dump_restore_due", return_value=True),
        patch.object(
            daemon,
            "record_local_dump_restore_success",
            record_success,
        ),
    ):
        if restore_mode:
            with patch.object(daemon, "_backup_loop", loop):
                _run_daemon()
        else:
            _run_daemon()
    (root / "state.json").write_text(
        json.dumps({"running": state.running, "success": state.last_success})
    )


def _run_daemon() -> None:
    daemon.install_graceful_shutdown("backup-shutdown-test")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(daemon.run())


def _wait_file(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 25
    while not path.exists():
        assert process.poll() is None, process.communicate(timeout=2)
        assert time.monotonic() < deadline, f"never reached {path.name}"
        time.sleep(0.02)


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _assert_sigterm_reaps_job_before_scheduler_exits(
    tmp_path: Path, mode: str, postgres_base: Path
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    retained = artifacts / "retained.dump.enc"
    retained.write_bytes(b"previous complete backup")
    code = (
        "from pathlib import Path; "
        "from tests.services.test_backup_scheduler_shutdown import _exercise_daemon; "
        f"_exercise_daemon(Path({str(tmp_path)!r}), {mode!r}, Path({str(postgres_base)!r}))"
    )
    process = subprocess.Popen(  # noqa: S603 -- fixed disposable scheduler harness
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _wait_file(tmp_path / "child", process)
        pids = [
            int(path.read_text())
            for name in ("worker", "child", "postgres")
            if (path := tmp_path / name).exists()
        ]
        assert (tmp_path / "daemon.pid").exists()
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=9)
        assert process.returncode == 0, stdout + stderr
        assert time.monotonic() - started < 9
        assert all(not _alive(pid) for pid in pids)
        assert not (tmp_path / "daemon.pid").exists()
        assert json.loads((tmp_path / "state.json").read_text()) == {
            "running": False,
            "success": None,
        }
        assert not (tmp_path / "restore-success").exists()
        assert retained.read_bytes() == b"previous complete backup"
        if mode not in {"stubborn", "restore-stubborn"}:
            assert not list(artifacts.glob("*.partial"))
            assert not (artifacts / "test.key").exists()
            assert not list(tmp_path.glob("ava-pg-*"))
            if (tmp_path / "pgdata").exists():
                assert not Path((tmp_path / "pgdata").read_text()).exists()
        if mode == "publish":
            assert len(list(artifacts.glob("*.dump.enc"))) == 2
    finally:
        # Clean only PIDs written by this disposable harness, including on the
        # negative control where the old scheduler leaves its executor blocked.
        for name in ("child", "worker", "postgres"):
            path = tmp_path / name
            if path.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(path.read_text()), signal.SIGKILL)
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize("mode", ["pg_dump", "backup encryption", "publish", "stubborn", "restore"])
def test_sigterm_reaps_job_before_scheduler_exits(
    tmp_path: Path, mode: str, postgres_base: Path
) -> None:
    _assert_sigterm_reaps_job_before_scheduler_exits(tmp_path, mode, postgres_base)


def test_killed_restore_uses_parent_owned_postgres_base(
    tmp_path: Path, postgres_base: Path
) -> None:
    _assert_sigterm_reaps_job_before_scheduler_exits(tmp_path, "restore-stubborn", postgres_base)
    assert Path((tmp_path / "pgdata").read_text()).is_relative_to(postgres_base)


def _write(path: Path) -> None:
    path.write_text("done")


def _postgres_roundtrip(marker: Path, postgres_base: Path) -> None:
    from shared.pg_tools import throwaway_postgres

    with throwaway_postgres(base=postgres_base, foreground=True):
        data = next(postgres_base.glob("ava-pg-*/data"))
        marker.write_text(data.joinpath("postmaster.pid").read_text().splitlines()[0])


def _crash() -> None:
    raise ValueError("job failed")


def _exit_without_result() -> None:
    os._exit(0)


def _fail_after_result() -> None:
    from multiprocessing.util import Finalize

    # Run after multiprocessing flushes its queue feeder, so the parent really
    # receives success before observing the contradictory nonzero exit status.
    Finalize(None, os._exit, args=(7,), exitpriority=-20)


async def test_worker_requires_successful_result_and_exit(tmp_path: Path) -> None:
    marker = tmp_path / "done"
    await run_job(partial(_write, marker))
    assert marker.read_text() == "done"
    with pytest.raises(RuntimeError, match="ValueError: job failed"):
        await run_job(_crash)
    with pytest.raises(RuntimeError, match="without a result"):
        await run_job(_exit_without_result)
    with pytest.raises(RuntimeError, match="exit=7"):
        await run_job(_fail_after_result)


async def test_worker_accepts_clean_native_postgres_exit(
    tmp_path: Path, postgres_base: Path
) -> None:
    marker = tmp_path / "postgres"
    await run_job(partial(_postgres_roundtrip, marker, postgres_base))
    assert not _alive(int(marker.read_text()))


async def test_cancel_before_adoption_cannot_start_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.backup_scheduler import worker

    pids: list[int] = []

    def cancel(_message: object, *, expected_pid: int) -> None:
        pids.append(expected_pid)
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "validate_ready_message", cancel)
    marker = tmp_path / "never-started"
    with pytest.raises(asyncio.CancelledError):
        await run_job(partial(_write, marker))
    assert not marker.exists()
    assert len(pids) == 1
    assert not _alive(pids[0])


def test_sigterm_bounded_exit_with_wedged_executor(tmp_path: Path) -> None:
    """SIGTERM exits within the bound while a wedged default-executor job stands."""
    # The exit bound only matters relative to the stop budget it protects:
    # assert the relationship, not just the number.
    assert EXIT_BOUND_S + KILL_SLACK_S < PAUSE_TIMEOUT_SECONDS / 5
    child = spawn_child(tmp_path, module="services.backup_scheduler.daemon", label="pg-backup")
    try:
        child.terminate()
        child.wait_bounded_exit(what="wedged executor job")
        assert "[pg-backup] interrupted, shutting down" in child.log_tail(), child.log_tail()
        assert "cleanup-ran" in child.markers(), (
            "the cancellation drain did not reach run()'s cleanup"
        )
    finally:
        child.close()
