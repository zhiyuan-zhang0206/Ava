from __future__ import annotations

import ast
import concurrent.futures
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from services.pitr import archive_shim
from shared.cluster.dataplane.pg_tools import pg_start_env


@pytest.mark.parametrize(
    "name",
    [
        "000000010000000000000001",
        "00000002.history",
        "000000010000000000000001.00000028.backup",
    ],
)
def test_archives_every_postgres_17_archive_filename_class(tmp_path: Path, name: str) -> None:
    source = tmp_path / name
    source.write_bytes(b"wal")
    spool = tmp_path / "spool"
    spool.mkdir()

    assert archive_shim.archive(source, name, spool, 1024) == 0
    assert (spool / name).read_bytes() == b"wal"
    assert not list(spool.glob("*.partial"))


@pytest.mark.parametrize(
    "name",
    ["../wal", "lowercase", "00000001.partial", "/tmp/wal"],  # noqa: S108
)
def test_rejects_unknown_or_traversing_names(tmp_path: Path, name: str) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"wal")
    spool = tmp_path / "spool"
    spool.mkdir()
    assert archive_shim.archive(source, name, spool, 1024) == archive_shim.EXIT_UNSAFE_PATH


def test_rejects_source_and_spool_symlinks(tmp_path: Path) -> None:
    name = "000000010000000000000001"
    real_source = tmp_path / name
    real_source.write_bytes(b"wal")
    source_link = tmp_path / "source-link"
    source_link.symlink_to(real_source)
    real_spool = tmp_path / "real-spool"
    real_spool.mkdir()
    spool_link = tmp_path / "spool"
    spool_link.symlink_to(real_spool, target_is_directory=True)

    assert (
        archive_shim.archive(source_link, name, real_spool, 1024) == archive_shim.EXIT_UNSAFE_PATH
    )
    assert (
        archive_shim.archive(real_source, name, spool_link, 1024) == archive_shim.EXIT_UNSAFE_PATH
    )


def test_existing_same_content_is_idempotent_and_collision_never_overwrites(tmp_path: Path) -> None:
    name = "000000010000000000000001"
    source = tmp_path / name
    source.write_bytes(b"first")
    spool = tmp_path / "spool"
    spool.mkdir()
    assert archive_shim.archive(source, name, spool, 1024) == 0
    assert archive_shim.archive(source, name, spool, 1024) == 0
    source.write_bytes(b"second")
    assert archive_shim.archive(source, name, spool, 1024) == archive_shim.EXIT_COLLISION
    assert (spool / name).read_bytes() == b"first"


def test_concurrent_publishers_never_replace(tmp_path: Path) -> None:
    name = "000000010000000000000001"
    source = tmp_path / name
    source.write_bytes(b"wal")
    spool = tmp_path / "spool"
    spool.mkdir()

    def publish(_: int) -> int:
        return archive_shim.archive(source, name, spool, 1024)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(publish, range(16)))
    assert results == [0] * 16
    assert (spool / name).read_bytes() == b"wal"


def test_hard_quota_refuses_without_deleting_unacked_files(tmp_path: Path) -> None:
    name = "000000010000000000000002"
    source = tmp_path / name
    source.write_bytes(b"new")
    spool = tmp_path / "spool"
    spool.mkdir()
    retained = spool / "000000010000000000000001"
    retained.write_bytes(b"retained")
    assert archive_shim.archive(source, name, spool, len(b"retained")) == archive_shim.EXIT_QUOTA
    assert retained.read_bytes() == b"retained"
    assert not (spool / name).exists()


def test_hard_quota_counts_crash_leftover_partial(tmp_path: Path) -> None:
    name = "000000010000000000000002"
    source = tmp_path / name
    source.write_bytes(b"new")
    spool = tmp_path / "spool"
    spool.mkdir()
    stale_partial = spool / ".000000010000000000000001.crash.partial"
    stale_partial.write_bytes(b"retained")

    assert archive_shim.archive(source, name, spool, len(b"retained")) == archive_shim.EXIT_QUOTA
    assert stale_partial.read_bytes() == b"retained"
    assert not (spool / name).exists()


@pytest.mark.parametrize("entry_kind", ["directory", "symlink", "fifo"])
@pytest.mark.parametrize("existing_target", [False, True])
def test_spool_refuses_non_regular_entries_fail_closed(
    tmp_path: Path, entry_kind: str, existing_target: bool
) -> None:
    name = "000000010000000000000002"
    source = tmp_path / name
    source.write_bytes(b"new")
    spool = tmp_path / "spool"
    spool.mkdir()
    target = spool / name
    if existing_target:
        target.write_bytes(source.read_bytes())
    entry = spool / "unexpected"
    if entry_kind == "directory":
        entry.mkdir()
    elif entry_kind == "symlink":
        symlink_target = tmp_path / "target"
        symlink_target.write_bytes(b"outside")
        entry.symlink_to(symlink_target)
    elif entry_kind == "fifo":
        os.mkfifo(entry)
    else:
        raise AssertionError(f"unknown fixture entry kind: {entry_kind}")

    assert archive_shim.archive(source, name, spool, 1024) == archive_shim.EXIT_UNSAFE_PATH
    assert os.path.lexists(entry)
    assert target.exists() is existing_target


def test_spool_refuses_fifo_archive_lock(tmp_path: Path) -> None:
    name = "000000010000000000000002"
    source = tmp_path / name
    source.write_bytes(b"new")
    spool = tmp_path / "spool"
    spool.mkdir()
    lock = spool / ".archive.lock"
    os.mkfifo(lock)

    assert archive_shim.archive(source, name, spool, 1024) == archive_shim.EXIT_UNSAFE_PATH
    assert stat.S_ISFIFO(lock.lstat().st_mode)
    assert not (spool / name).exists()


def test_failed_publish_removes_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    name = "000000010000000000000001"
    source = tmp_path / name
    source.write_bytes(b"wal")
    spool = tmp_path / "spool"
    spool.mkdir()

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk")

    monkeypatch.setattr(archive_shim.os, "link", fail_link)
    assert archive_shim.archive(source, name, spool, 1024) == archive_shim.EXIT_IO
    assert not list(spool.glob("*.partial"))


def _postmaster_run(command: str, *, cwd: Path, **placeholders: str) -> None:
    """Run a PITR command the way the postmaster does: `/bin/sh -c` in the data
    directory, placeholders substituted, under the postmaster's environment."""
    for placeholder, value in placeholders.items():
        command = command.replace(f"%{placeholder}", value)
    completed = subprocess.run(  # noqa: S603 — the command under test, fixed shell
        ["/bin/sh", "-c", command],
        cwd=cwd,
        env=pg_start_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_the_postmaster_environment_runs_both_pitr_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The postmaster keeps only the operator's process mechanics
    (`pg_start_env`). That is enough for the commands it runs: the archive shim
    finds `python3` through PATH, and the restore drill's command names its
    interpreter by absolute path. Both take everything else on argv."""
    from services.pitr.activation.runtime import desired_archive_settings
    from services.pitr.restore.postgres import _append_recovery_config

    # The raw process environment a spawned child would inherit (Settings is
    # not involved): what the `ava start` that spawns the postmaster may hold.
    held = {
        "AVA_API_TOKEN": "gateway-api-token-" + "t" * 32,
        "AVA_CLUSTER_SECRET": "human-" + "h" * 40,
    }
    for key, value in held.items():
        monkeypatch.setenv(key, value)
    assert not [name for name in pg_start_env() if name.startswith("AVA_")]
    name = "000000010000000000000001"

    home = tmp_path / "home"
    shim = home / "runtime" / "pg-archive" / "archive-shim"
    shim.parent.mkdir(parents=True)
    shutil.copyfile(Path(archive_shim.__file__), shim)
    shim.chmod(0o700)
    spool = home / "physical-backup" / "spool"
    spool.mkdir(parents=True)
    live = tmp_path / "live"
    (live / "pg_wal").mkdir(parents=True)
    (live / "pg_wal" / name).write_bytes(b"wal")
    archive_command = desired_archive_settings(home)["archive_command"]
    _postmaster_run(archive_command, cwd=live, p=f"pg_wal/{name}", f=name)
    assert (spool / name).read_bytes() == b"wal"

    run_root = tmp_path / "restore"
    sandbox, archive, socket_dir = run_root / "data", run_root / "archive", run_root / "socket"
    for directory in (sandbox, archive, socket_dir):
        directory.mkdir(parents=True)
    (archive / name).write_bytes(b"wal")
    _append_recovery_config(sandbox, archive, socket_dir, "0/200", run_root)
    [line] = [
        line
        for line in (sandbox / "postgresql.auto.conf").read_text().splitlines()
        if line.startswith("restore_command = ")
    ]
    restore_command = ast.literal_eval(line.partition(" = ")[2])
    _postmaster_run(restore_command, cwd=sandbox, f=name, p="pg_wal/RECOVERYXLOG")
    assert (sandbox / "pg_wal" / "RECOVERYXLOG").read_bytes() == b"wal"
