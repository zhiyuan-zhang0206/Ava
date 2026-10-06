"""Backup worker happy path, cancellation, private cleanup and result validation."""

from __future__ import annotations

import asyncio
import hashlib
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.backup.scheduler.operation import worker_process as workers
from services.backup.scheduler.tests.operation_support import operation_kind, stub_worker, until

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="operation workers require POSIX")


async def test_zero_exit_valid_result_is_committed_and_staging_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    children = stub_worker(tmp_path, monkeypatch, "Path(sys.argv[2]).write_text('{\"ok\":true}')\n")
    completed = await workers.run_operation("unused", {}, kind=operation_kind(tmp_path), env={})
    assert completed.result == {"ok": True} and children[0].returncode == 0
    assert completed.work.stat().st_mode & 0o777 == 0o700
    assert (completed.work / "request.json").stat().st_mode & 0o777 == 0o600
    assert await completed.commit(lambda: "published") == "published"
    assert not completed.work.exists()


@pytest.mark.parametrize("payload,code", [("{}", 3), ("invalid", 0), ("[]", 0), (None, 0)])
async def test_worker_failure_cleans_staging_without_blocking_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str | None, code: int
) -> None:
    write = "" if payload is None else f"Path(sys.argv[2]).write_text({payload!r});"
    children = stub_worker(tmp_path, monkeypatch, f"{write}sys.exit({code})\n")
    for _ in range(2):
        with pytest.raises((RuntimeError, TypeError)):
            await workers.run_operation("unused", {}, kind=operation_kind(tmp_path), env={})
        assert not list((tmp_path / "controls").glob(".operation-*"))
    assert len(children) == 2
    assert not list(tmp_path.rglob("unresolved.json"))


async def test_cancel_unwinds_worker_plaintext_cleanup_and_releases_kind_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready, plaintext = tmp_path / "ready", tmp_path / "plaintext"
    children = stub_worker(
        tmp_path,
        monkeypatch,
        "import signal\n"
        "def stopped(*_args): raise KeyboardInterrupt\n"
        "signal.signal(signal.SIGTERM,stopped)\n"
        f"Path({str(plaintext)!r}).write_text('private')\n"
        f"Path({str(ready)!r}).touch()\n"
        "try: time.sleep(60)\n"
        f"finally: Path({str(plaintext)!r}).unlink()\n",
    )
    task = asyncio.create_task(
        workers.run_operation("unused", {}, kind=operation_kind(tmp_path), env={})
    )
    try:
        await until(ready)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[0].returncode is not None
        assert not plaintext.exists()
        assert not list((tmp_path / "controls").glob(".operation-*"))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


async def test_timeout_bounds_worker_and_sanitizes_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    children = stub_worker(tmp_path, monkeypatch, "time.sleep(60)\n")
    cleaned: list[Path] = []
    with pytest.raises(TimeoutError, match="execution bound"):
        await workers.run_operation(
            "unused",
            {},
            kind=operation_kind(tmp_path, sanitize=cleaned.append),
            env={},
            timeout_s=0.05,
        )
    assert children[0].returncode is not None
    assert len(cleaned) == 1 and not cleaned[0].exists()


async def test_failed_sanitizer_reports_private_staging_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_worker(tmp_path, monkeypatch, "Path(sys.argv[2]).write_text('{}')\n")

    def failed(_work: Path) -> None:
        raise RuntimeError("plaintext cleanup failed")

    completed = await workers.run_operation(
        "unused", {}, kind=operation_kind(tmp_path, sanitize=failed), env={}
    )
    with pytest.raises(RuntimeError, match="plaintext cleanup failed") as caught:
        await completed.commit(lambda: None)
    assert completed.work.exists()
    assert str(completed.work) in "\n".join(caught.value.__notes__)


async def test_old_receipts_do_not_block_independent_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = tmp_path / "controls/.operation-old"
    old.mkdir(parents=True)
    (old / "unresolved.json").write_text("{}")
    stub_worker(tmp_path, monkeypatch, "Path(sys.argv[2]).write_text('{}')\n")
    completed = await workers.run_operation("unused", {}, kind=operation_kind(tmp_path), env={})
    assert completed.work != old
    await completed.commit(lambda: None)
    assert old.exists(), "old private scratch is not adopted or silently deleted"


async def test_scheduled_commit_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.backup.scheduler import worker

    name = "ava-20260926T000000Z.dump.enc"
    stub_worker(
        tmp_path,
        monkeypatch,
        f"artifact=Path(sys.argv[2]).parent/'artifact'/{name!r}\n"
        "artifact.parent.mkdir();artifact.write_bytes(b'encrypted')\n"
        "Path(sys.argv[2]).write_text(json.dumps(dict(artifact=artifact.name,sha256='d')))\n",
    )
    monkeypatch.setattr(worker, "ava_home", lambda: tmp_path)
    ticks = 0
    during: list[int] = []

    def slow_commit(staged: Path, _digest: str) -> Path:
        started = ticks
        time.sleep(0.2)
        during.append(ticks - started)
        return staged

    monkeypatch.setattr(worker, "commit_scheduled_backup", slow_commit)

    async def tick() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(tick())
    try:
        await worker.run_job("dump", now=datetime(2026, 9, 26, tzinfo=UTC))
    finally:
        ticker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ticker
    assert during and during[0] >= 5


async def test_failed_operation_sanitizer_keeps_path_and_reports_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    stub_worker(tmp_path, monkeypatch, "sys.exit(3)\n")

    def failed(_work: Path) -> None:
        raise RuntimeError("cannot sanitize private material")

    with pytest.raises(RuntimeError, match="operation exited 3") as caught:
        await workers.run_operation(
            "unused", {}, kind=operation_kind(tmp_path, sanitize=failed), env={}
        )
    [work] = (tmp_path / "controls").glob(".operation-*")
    assert str(work) in caplog.text
    assert "cannot sanitize private material" in caplog.text
    assert str(work) in "\n".join(caught.value.__notes__)


async def test_worker_secrets_and_progress_never_touch_retained_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live URL arrives on stdin; stderr progress reaches the operator live."""
    from services.backup.scheduler.operation import worker_process

    stub_worker(
        tmp_path,
        monkeypatch,
        "import hashlib\nfrom services.backup.scheduler.operation.worker_process import worker_secrets\n"
        "seen=hashlib.sha256(worker_secrets()['live_db_url'].encode()).hexdigest()\n"
        "sys.stderr.write('downloading base\\nbase extracted\\n');sys.stderr.flush()\n"
        "Path(sys.argv[2]).write_text(json.dumps({'seen':seen}))\n",
    )
    lines: list[str] = []
    secret = "postgresql://ava:s3cret@127.0.0.1:5433/ava"  # noqa: S105 -- fixture
    completed = await worker_process.run_operation(
        "m",
        {"chain": "c"},
        kind=operation_kind(tmp_path),
        env={},
        secrets={"live_db_url": secret},
        progress=lines.append,
    )
    assert completed.result == {"seen": hashlib.sha256(secret.encode()).hexdigest()}
    assert lines == ["downloading base", "base extracted"]
    retained = b"".join(path.read_bytes() for path in completed.work.rglob("*") if path.is_file())
    assert b"s3cret" not in retained
    await completed.commit(lambda: None)


async def test_busy_concurrency_lock_defers_without_creating_staging(tmp_path: Path) -> None:
    from base.native_process.os_platform import file_lock
    from services.backup.scheduler.operation.staging import OperationBusyError

    kind = operation_kind(tmp_path)
    kind.control_root.mkdir()
    with file_lock(kind.control_root / ".lock"), pytest.raises(OperationBusyError):
        await workers.run_operation("unused", {}, kind=kind, env={})
    assert not list(kind.control_root.glob(".operation-*"))
