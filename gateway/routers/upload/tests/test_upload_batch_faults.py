"""Hard process death and database-disconnected file writers, using real Postgres."""

import json
import subprocess
import sys
import threading
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine import machine_name
from base.host.private_storage import ensure_private_dir
from gateway.routers.upload import batches as upload_batches
from gateway.routers.upload.batches import UploadItem
from gateway.schemas.uploads import UploadedBatch

_CHILD = """
import json, os, sys
from pathlib import Path
from psycopg_pool import ConnectionPool
config = json.load(sys.stdin)
os.environ["AVA_HOME"] = config["home"]
os.environ["AVA_CONFIG_FETCH"] = "skip"
from base.cluster.machine import set_identity
set_identity(name=config["machine"], role=["gateway", "agent-runner"])
from gateway.routers.upload import batches as owner
directory = Path(config["directory"])
original_publish = owner.publish_files
original_link = os.link
def link(source, target):
    original_link(source, target)
    if Path(target).parent == directory and config["phase"] == "first-file":
        os._exit(41)
def publish(directory, manifest, batch):
    if config["phase"] == "receiving":
        os._exit(41)
    original_publish(directory, manifest, batch)
    if config["phase"] == "published":
        os._exit(41)
os.link = link
owner.publish_files = publish
with ConnectionPool(config["dsn"], min_size=1, max_size=1) as pool:
    owner.save_keyed_batch(pool, config.get("key", "crash"), 123, directory,
        [("a.png", b"aaa", "image/png"), ("b.png", b"bbb", "image/png")],
        ["a.png", "b.png"], deliver=False, max_bytes=6, max_files=2)
os._exit(41)
"""


def _save(pool: ConnectionPool, directory: Path) -> UploadedBatch:
    return upload_batches.save_keyed_batch(
        pool,
        "crash",
        123,
        directory,
        [("a.png", b"aaa", "image/png"), ("b.png", b"bbb", "image/png")],
        ["a.png", "b.png"],
        deliver=False,
        max_bytes=6,
        max_files=2,
    )


@pytest.fixture
def batch_target(db_conn: psycopg.Connection, tmp_path: Path) -> Path:
    db_conn.execute("INSERT INTO agents (id) VALUES (123)")
    db_conn.execute("INSERT INTO agents_meta (id, status) VALUES (123, 'idling')")
    db_conn.commit()
    ensure_private_dir(tmp_path / "writer-home")
    return ensure_private_dir(tmp_path / "Downloads" / "AvaAgent-123")


@pytest.mark.parametrize("phase", ["receiving", "first-file", "published", "ready"])
def test_hard_death_recovers_fixed_objects(
    db_conn: psycopg.Connection, batch_target: Path, phase: str
) -> None:
    child = subprocess.run(  # noqa: S603 -- fixed Python program against isolated test DB
        [sys.executable, "-c", _CHILD],
        input=json.dumps(
            {
                "dsn": db_conn.info.dsn,
                "directory": str(batch_target),
                "phase": phase,
                "home": str(batch_target.parent.parent / "writer-home"),
                "machine": machine_name(),
            }
        ),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 41, child.stderr
    row = db_conn.execute("SELECT manifest, receipt FROM agent_upload_batches").fetchone()
    assert row is not None
    names = [item["stored_name"] for item in row[0]]
    assert (row[1] is not None) == (phase == "ready")
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn, min_size=1, max_size=2) as pool:
        receipt = _save(pool, batch_target)
        assert _save(pool, batch_target) == receipt
    assert [Path(file.path).name for file in receipt.files] == names
    assert [Path(file.path).read_bytes() for file in receipt.files] == [b"aaa", b"bbb"]
    assert len([p for p in batch_target.iterdir() if p.is_file()]) == 2


def test_disconnected_writer_can_finish_after_recovery_without_overwriting(
    db_conn: psycopg.Connection,
    batch_target: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_transaction = upload_batches.write_transaction
    original_publish = upload_batches.publish_files
    blocked = threading.Event()
    release = threading.Event()
    pid: list[int] = []
    writer_thread: list[int] = []

    @contextmanager
    def transaction(pool: ConnectionPool) -> Generator[psycopg.Connection, None, None]:
        with original_transaction(pool) as conn:
            if threading.get_ident() == writer_thread[0]:
                pid[:] = [conn.info.backend_pid]
            yield conn

    def publish(directory: Path, manifest: list[dict[str, Any]], batch: list[UploadItem]) -> None:
        if threading.get_ident() == writer_thread[0]:
            blocked.set()
            assert release.wait(10)
        original_publish(directory, manifest, batch)

    def old_writer(pool: ConnectionPool) -> UploadedBatch:
        writer_thread.append(threading.get_ident())
        return _save(pool, batch_target)

    monkeypatch.setattr(upload_batches, "write_transaction", transaction)
    monkeypatch.setattr(upload_batches, "publish_files", publish)
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn, min_size=1, max_size=3) as pool:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(old_writer, pool)
            try:
                assert blocked.wait(10)
                db_conn.execute("SELECT pg_terminate_backend(%s)", (pid[0],))
                db_conn.commit()
                recovered = _save(pool, batch_target)
            finally:
                release.set()
            with pytest.raises(psycopg.OperationalError):
                future.result(timeout=10)
        assert _save(pool, batch_target) == recovered
    assert [Path(file.path).read_bytes() for file in recovered.files] == [b"aaa", b"bbb"]
    row = db_conn.execute("SELECT count(*) FROM agent_upload_batches").fetchone()
    assert row is not None and row[0] == 1


def test_independent_processes_share_quota(db_conn: psycopg.Connection, batch_target: Path) -> None:
    children: list[subprocess.Popen[str]] = []
    try:
        for key in ("one", "two"):
            child = subprocess.Popen(  # noqa: S603 -- fixed program against isolated test DB
                [sys.executable, "-c", _CHILD],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert child.stdin is not None
            child.stdin.write(
                json.dumps(
                    {
                        "dsn": db_conn.info.dsn,
                        "directory": str(batch_target),
                        "phase": "ready",
                        "key": key,
                        "home": str(batch_target.parent.parent / "writer-home"),
                        "machine": machine_name(),
                    }
                )
            )
            child.stdin.close()
            children.append(child)
        codes = [child.wait(timeout=30) for child in children]
        assert sorted(codes) == [1, 41]
        loser = next(child for child in children if child.returncode == 1)
        assert loser.stderr is not None
        assert "413" in loser.stderr.read()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
    row = db_conn.execute("SELECT count(*) FROM agent_upload_batches").fetchone()
    assert row is not None and row[0] == 1


def test_receipts_use_explicit_writable_transactions(
    db_conn: psycopg.Connection,
    batch_target: Path,
) -> None:
    with ConnectionPool[psycopg.Connection](
        db_conn.info.dsn,
        min_size=1,
        max_size=1,
        kwargs={"options": "-c default_transaction_read_only=on"},
    ) as pool:
        assert len(_save(pool, batch_target).files) == 2
