"""The commit, cleanup and durability contract of `base.host.atomic_io`.

Failures are caused through an `AtomicWriter` built over the `Fs` a test needs; the module
functions that every other component calls are the same writer over the real calls.
"""

from __future__ import annotations

import dataclasses
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from base.host import atomic_io
from base.host.atomic_io import SYSTEM_FS, AtomicWriter, Fs


def _recording_fs(events: list[str]) -> Fs:
    """The real calls, each noting what it was asked to do first."""

    def replace(source: Path, target: Path) -> None:
        events.append(f"replace {source.name} -> {target.name}")
        SYSTEM_FS.replace(source, target)

    def fsync(fd: int) -> None:
        events.append("fsync parent" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync file")
        SYSTEM_FS.fsync(fd)

    def fchmod(fd: int, mode: int) -> None:
        events.append(f"fchmod {mode:o}")
        SYSTEM_FS.fchmod(fd, mode)

    return Fs(replace=replace, fsync=fsync, fchmod=fchmod)


def test_bytes_are_mode_set_file_synced_committed_then_parent_synced(tmp_path: Path) -> None:
    path = tmp_path / "bytes.bin"
    events: list[str] = []
    AtomicWriter(_recording_fs(events)).write_bytes(
        path, b"\x00payload", mode=0o600, sync_parent=True
    )
    assert events[0] == "fchmod 600"
    assert events[1] == "fsync file"
    assert events[2].startswith("replace .bytes.bin.") and events[2].endswith(".tmp -> bytes.bin")
    assert events[3] == "fsync parent"
    assert path.read_bytes() == b"\x00payload"
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(tmp_path.iterdir()) == [path]


def test_sync_can_be_skipped_without_skipping_the_atomic_replace(tmp_path: Path) -> None:
    path = tmp_path / "bytes.bin"
    path.write_bytes(b"old")
    events: list[str] = []
    AtomicWriter(_recording_fs(events)).write_bytes(path, b"new", sync_file=False)
    assert [event.split()[0] for event in events] == ["replace"]
    assert path.read_bytes() == b"new"
    assert sorted(tmp_path.iterdir()) == [path]


def test_prefix_and_suffix_name_the_sibling_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    events: list[str] = []
    writer = AtomicWriter(_recording_fs(events))
    writer.write_text(path, "a")
    writer.write_text(path, "b", prefix="pending-", suffix=".part")
    first, second = (
        event.split(" -> ")[0].removeprefix("replace ") for event in events if "replace" in event
    )
    assert first.startswith(".config.yml.") and first.endswith(".tmp")
    assert second.startswith("pending-") and second.endswith(".part")


@pytest.mark.parametrize("as_text", [False, True])
def test_a_failed_replace_keeps_the_old_content_and_removes_the_temporary_file(
    as_text: bool, tmp_path: Path
) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"old")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace failure")

    writer = AtomicWriter(dataclasses.replace(SYSTEM_FS, replace=fail_replace))
    with pytest.raises(OSError, match="replace failure"):
        if as_text:
            writer.write_text(path, "new")
        else:
            writer.write_bytes(path, b"new")
    assert path.read_bytes() == b"old"
    assert sorted(tmp_path.iterdir()) == [path]


def test_a_failed_file_sync_never_commits(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"old")
    events: list[str] = []

    def fail_sync(_fd: int) -> None:
        raise OSError("file sync failure")

    writer = AtomicWriter(
        dataclasses.replace(_recording_fs(events), fsync=fail_sync),
    )
    with pytest.raises(OSError, match="file sync failure"):
        writer.write_bytes(path, b"new")
    assert events == []
    assert path.read_bytes() == b"old"
    assert sorted(tmp_path.iterdir()) == [path]


def test_a_failed_parent_sync_leaves_the_new_content_committed(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"old")

    def sync_files_only(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory sync failure")
        SYSTEM_FS.fsync(fd)

    writer = AtomicWriter(dataclasses.replace(SYSTEM_FS, fsync=sync_files_only))
    with pytest.raises(OSError, match="directory sync failure"):
        writer.write_bytes(path, b"new", sync_parent=True)
    assert path.read_bytes() == b"new"
    assert sorted(tmp_path.iterdir()) == [path]
    with pytest.raises(OSError, match="directory sync failure"):
        writer.fsync_parent(path)


def test_a_failed_mode_change_removes_the_temporary_file_and_never_replaces(
    tmp_path: Path,
) -> None:
    path = tmp_path / "secret"
    path.write_bytes(b"old")
    events: list[str] = []

    def fail_chmod(_fd: int, _mode: int) -> None:
        raise OSError("chmod failure")

    writer = AtomicWriter(dataclasses.replace(_recording_fs(events), fchmod=fail_chmod))
    with pytest.raises(OSError, match="chmod failure"):
        writer.write_text(path, "new", mode=0o600)
    assert events == []
    assert path.read_bytes() == b"old"
    assert sorted(tmp_path.iterdir()) == [path]


def test_the_directory_is_synced_only_when_asked(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    events: list[str] = []
    writer = AtomicWriter(_recording_fs(events))
    writer.write_text(path, "café", encoding="utf-8")
    assert "fsync parent" not in events
    assert path.read_bytes() == "café".encode()
    writer.write_text(path, "again", sync_parent=True)
    assert events[-1] == "fsync parent"


def test_the_temporary_file_has_its_final_mode_when_it_is_published(tmp_path: Path) -> None:
    path = tmp_path / "secret"
    modes: list[int] = []

    def replace(source: Path, target: Path) -> None:
        modes.append(stat.S_IMODE(source.stat().st_mode))
        SYSTEM_FS.replace(source, target)

    AtomicWriter(dataclasses.replace(SYSTEM_FS, replace=replace)).write_text(
        path, "token", mode=0o600
    )
    assert modes == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_concurrent_writers_publish_through_distinct_sibling_temporaries(tmp_path: Path) -> None:
    path = tmp_path / "config.txt"
    barrier = Barrier(2)
    sources: list[Path] = []

    def rendezvous(source: Path, target: Path) -> None:
        sources.append(source)
        barrier.wait(timeout=5)
        SYSTEM_FS.replace(source, target)

    writer = AtomicWriter(dataclasses.replace(SYSTEM_FS, replace=rendezvous))
    values = ["first" * 4096, "second" * 4096]

    def write(value: str) -> None:
        writer.write_text(path, value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, values))

    assert path.read_text() in values
    assert len(set(sources)) == 2
    assert all(source.parent == path.parent for source in sources)
    assert all(
        source.name.startswith(f".{path.name}.") and source.suffix == ".tmp" for source in sources
    )
    assert sorted(tmp_path.iterdir()) == [path]


def test_text_is_encoded_as_requested(tmp_path: Path) -> None:
    path = tmp_path / "pointer.txt"
    AtomicWriter(SYSTEM_FS).write_text(path, "café", encoding="utf-16-le")
    assert path.read_bytes() == "café".encode("utf-16-le")


def test_module_functions_write_through_the_real_calls(tmp_path: Path) -> None:
    text = tmp_path / "pointer.pth"
    atomic_io.write_text_atomic(text, "target", mode=0o644, sync_file=False)
    assert text.read_bytes() == b"target"
    assert stat.S_IMODE(text.stat().st_mode) == 0o644
    data = tmp_path / "bytes.bin"
    atomic_io.write_bytes_atomic(data, b"\x00payload", mode=0o600, sync_parent=True)
    assert data.read_bytes() == b"\x00payload"
    assert stat.S_IMODE(data.stat().st_mode) == 0o600
    atomic_io.fsync_parent(data)
    assert sorted(tmp_path.iterdir()) == [data, text]
