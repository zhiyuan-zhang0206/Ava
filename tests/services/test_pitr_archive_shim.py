from __future__ import annotations

import concurrent.futures
from pathlib import Path

import pytest

from services.pitr import archive_shim


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
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: archive_shim.archive(source, name, spool, 1024), range(16))
        )
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


def test_failed_publish_removes_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    name = "000000010000000000000001"
    source = tmp_path / name
    source.write_bytes(b"wal")
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(
        archive_shim.os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk"))
    )
    assert archive_shim.archive(source, name, spool, 1024) == archive_shim.EXIT_IO
    assert not list(spool.glob("*.partial"))
