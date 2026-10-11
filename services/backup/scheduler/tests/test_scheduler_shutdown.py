"""Real signals stop the scheduler's worker and clean its private staging.

The controller gives the known worker/group a bounded chance to unwind before
escalating. These tests exercise blocking backup stages and real temporary
Postgres startup/shutdown; they do not prove every descendant disappeared.
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
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import psutil
import pytest

from base.config import ConfigBoot, ensure_eager, settings
from base.db import Database
from base.native_process.loaded_commit import LoadedCommit
from services.backup.scheduler import daemon, worker
from services.backup.scheduler.operation.worker_process import CompletedOperation, StopSignal

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


def _backup_patches(root: Path, mode: str) -> contextlib.ExitStack:
    """Run the real scheduled preparation with one blocking pipeline stage."""
    from services.backup import dump as backup
    from services.backup.artifact import offsite

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

    def publish(*_args: object, **_kwargs: object) -> None:
        _block(root, mode)

    stack = contextlib.ExitStack()
    for patcher in (
        patch.object(backup, "backup_dir", return_value=root / "artifacts"),
        patch.object(backup, "dump_source", return_value="postgresql://ava@127.0.0.1:1/test"),
        patch.object(backup, "pg_tool", return_value=Path("pg_dump")),
        patch.object(backup, "_db_size_breakdown", return_value="test"),
        patch.object(backup, "_run_with_progress", run),
        patch.object(backup, "_key_file", key),
        patch.object(offsite, "publish", publish),
    ):
        stack.enter_context(patcher)
    return stack


def _restore(root: Path, mode: str, postgres_base: Path) -> None:
    from base.cluster.dataplane.pg_tools import throwaway_postgres

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
        if mode != "roundtrip":
            _block(root, mode)


def _exercise_job(root: Path, mode: str, postgres_base: Path) -> None:
    """The operation worker's real entry, with only its external effects patched."""
    from scripts.data_plane_ops import restore_drill

    def restore(
        *, database_for_url: Callable[[str], Database], foreground: bool, scratch_root: Path
    ) -> None:
        assert foreground
        (scratch_root / "backup.dump").write_bytes(b"PLAINTEXT")
        _restore(root, "stubborn" if mode == "restore-stubborn" else mode, postgres_base)

    with _backup_patches(root, mode), patch.object(restore_drill, "run_drill", restore):
        worker.main()


def _operation_harness(root: Path, mode: str, postgres_base: Path):
    run = worker.run_operation

    async def execute(
        module: str,
        request: Mapping[str, object],
        *,
        kind: worker.OperationKind,
        env: dict[str, str],
        secrets: Mapping[str, str] | None = None,
        stop: StopSignal | None = None,
        timeout_s: float = 6 * 3600,
    ) -> CompletedOperation:
        assert module == "services.backup.scheduler.worker"
        # Only the trusted worker entry changes. Production bootstrap, launch,
        # logs, process group and cancellation execute without private patches.
        return await run(
            "services.backup.scheduler.tests.shutdown_worker",
            request,
            kind=kind,
            env={
                **env,
                "AVA_TEST_BACKUP_ROOT": str(root),
                "AVA_TEST_BACKUP_MODE": mode,
                "AVA_TEST_BACKUP_POSTGRES_BASE": str(postgres_base),
            },
            secrets=secrets,
            stop=stop,
            timeout_s=timeout_s,
        )

    return execute


def _exercise_daemon(root: Path, mode: str, postgres_base: Path) -> None:
    # This child is not pytest: patching a settings attribute needs the eager chain.
    ensure_eager()
    state = daemon._BackupState()
    pidfile = root / "daemon.pid"
    restore_mode = mode.startswith("restore")

    def record_success(_now: datetime) -> None:
        (root / "restore-success").touch()

    async def loop(_state: object, *, config: ConfigBoot) -> None:
        if restore_mode:
            await daemon._run_due_local_dump_restore(datetime.now().astimezone(), config=config)
        else:
            await daemon._backup_loop(state, config=config)

    with (
        patch.object(daemon, "_is_running", return_value=False),
        patch.object(daemon, "_write_pidfile", lambda: pidfile.write_text(str(os.getpid()))),
        patch.object(daemon, "_remove_pidfile", lambda: pidfile.unlink(missing_ok=True)),
        patch.object(daemon, "start_health_server", AsyncMock(return_value=object())),
        patch.object(daemon, "stop_health_server", AsyncMock()),
        patch.object(daemon, "is_due", return_value=True),
        patch.object(worker, "ava_home", return_value=root),
        patch.object(settings.data_plane, "pg_throwaway_base", str(postgres_base)),
        patch.object(worker, "run_operation", _operation_harness(root, mode, postgres_base)),
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
        asyncio.run(daemon.run(config=ConfigBoot(), image=LoadedCommit(Path(), None)))


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


def _terminate_and_assert_reaped(tmp_path: Path, process: subprocess.Popen[str]) -> None:
    _wait_file(tmp_path / "child", process)
    pids = [
        int(path.read_text()) for name in ("worker", "child") if (path := tmp_path / name).exists()
    ]
    assert (tmp_path / "daemon.pid").exists()
    started = time.monotonic()
    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=9)
    assert process.returncode == 0, stdout + stderr
    assert time.monotonic() - started < 9
    assert all(not _alive(pid) for pid in pids)


def _assert_backup_artifacts(tmp_path: Path, artifacts: Path) -> None:
    # Cancellation never publishes the new artifact or retains decrypted
    # staging. The previously published backup remains untouched.
    assert not list((tmp_path / "backups" / "operations").glob("*/.operation-*"))
    assert not (tmp_path / "backups" / "quarantine").exists()
    assert [path.name for path in artifacts.iterdir()] == ["retained.dump.enc"]


def _kill_harness_processes(tmp_path: Path, process: subprocess.Popen[str]) -> None:
    # Clean only PIDs written by this disposable harness, including on a
    # negative control where a scheduler leaves its job blocked.
    for name in ("child", "worker", "postgres"):
        path = tmp_path / name
        if path.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(path.read_text()), signal.SIGKILL)
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=5)


def _assert_sigterm_reaps_job_before_scheduler_exits(
    tmp_path: Path, mode: str, postgres_base: Path
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    retained = artifacts / "retained.dump.enc"
    retained.write_bytes(b"previous complete backup")
    code = (
        "from pathlib import Path; "
        "from services.backup.scheduler.tests.test_scheduler_shutdown import _exercise_daemon; "
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
        _terminate_and_assert_reaped(tmp_path, process)
        assert not (tmp_path / "daemon.pid").exists()
        assert json.loads((tmp_path / "state.json").read_text()) == {
            "running": False,
            "success": None,
        }
        assert not (tmp_path / "restore-success").exists()
        assert retained.read_bytes() == b"previous complete backup"
        _assert_backup_artifacts(tmp_path, artifacts)
    finally:
        _kill_harness_processes(tmp_path, process)


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
    # The killed worker bypassed its context manager. The sanitizer uses the
    # existing temporary-cluster owner lock and native shutdown to clean it.
    assert not list(postgres_base.glob("ava-pg-*"))


async def test_restore_job_accepts_clean_foreground_postgres_exit(
    tmp_path: Path, postgres_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real foreground postmaster that stops cleanly lets the job succeed."""
    monkeypatch.setattr(worker, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(
        worker, "run_operation", _operation_harness(tmp_path, "roundtrip", postgres_base)
    )
    await worker.run_job("restore", config=ConfigBoot())
    assert not _alive(int((tmp_path / "postgres").read_text()))
    controls = tmp_path / "backups" / "operations" / "restore-drill"
    assert [path.name for path in controls.iterdir()] == [".lock"]
