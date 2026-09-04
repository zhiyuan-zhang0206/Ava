"""No-follow filesystem primitives for the external operator skill bridge."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

_MARKER_NAME = ".ava-managed.json"


class _SourceIntegrityError(RuntimeError):
    """The repository source cannot safely be copied."""


class _ClientConflictError(RuntimeError):
    """A user-owned client path cannot safely be changed."""


def _attributes_reparse(current: os.stat_result) -> bool:
    attribute = getattr(current, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attribute & flag)


def _lstat(path: Path) -> os.stat_result:
    current = path.lstat()
    if stat.S_ISLNK(current.st_mode) or _attributes_reparse(current):
        raise _ClientConflictError("linked or reparse filesystem component")
    return current


def _source_lstat(path: Path) -> os.stat_result:
    try:
        current = path.lstat()
    except OSError as exc:
        raise _SourceIntegrityError("operator skill source cannot be inspected") from exc
    if stat.S_ISLNK(current.st_mode) or _attributes_reparse(current):
        raise _SourceIntegrityError("operator skill source contains a linked entry")
    return current


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _signature(current: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        current.st_dev,
        current.st_ino,
        current.st_mode,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    )


def _read_regular(path: Path, *, source: bool) -> tuple[bytes, int]:
    inspect = _source_lstat if source else _lstat
    before = inspect(path)
    error = _SourceIntegrityError if source else _ClientConflictError
    if not stat.S_ISREG(before.st_mode):
        raise error("operator skill tree contains a non-regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if _signature(opened) != _signature(before):
                raise error("operator skill tree changed while being read")
            data = stream.read()
    except OSError as exc:
        raise error("operator skill tree cannot be read safely") from exc
    after = inspect(path)
    if _signature(after) != _signature(before):
        raise error("operator skill tree changed while being read")
    return data, stat.S_IMODE(before.st_mode)


def _tree_digest(
    root: Path, *, source: bool = False, ignore_root_names: frozenset[str] = frozenset()
) -> str:
    """Hash the validated manifest without following filesystem links."""
    return _manifest_digest(
        _tree_manifest(root, source=source, ignore_root_names=ignore_root_names)
    )


def _manifest_digest(manifest: list[dict[str, Any]]) -> str:
    encoded = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _tree_manifest(
    root: Path, *, source: bool = False, ignore_root_names: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """Describe each path Ava may later verify or reclaim."""
    inspect = _source_lstat if source else _lstat
    error = _SourceIntegrityError if source else _ClientConflictError
    root_before = inspect(root)
    if not stat.S_ISDIR(root_before.st_mode):
        raise error("operator skill tree root is not a directory")
    manifest: list[dict[str, Any]] = [
        {"kind": "directory", "mode": stat.S_IMODE(root_before.st_mode), "path": "."}
    ]

    def visit(directory: Path) -> None:
        before = inspect(directory)
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise error("operator skill tree cannot be enumerated safely") from exc
        for child in children:
            if directory == root and child.name in ignore_root_names:
                continue
            current = inspect(child)
            relative = child.relative_to(root).as_posix()
            if stat.S_ISDIR(current.st_mode):
                manifest.append(
                    {
                        "kind": "directory",
                        "mode": stat.S_IMODE(current.st_mode),
                        "path": relative,
                    }
                )
                visit(child)
            elif stat.S_ISREG(current.st_mode):
                data, mode = _read_regular(child, source=source)
                manifest.append(
                    {
                        "kind": "file",
                        "mode": mode,
                        "path": relative,
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
            else:
                raise error("operator skill tree contains an unsupported entry")
        after = inspect(directory)
        if _signature(after) != _signature(before):
            raise error("operator skill tree changed while being inspected")

    visit(root)
    after = inspect(root)
    if _signature(after) != _signature(root_before):
        raise error("operator skill tree changed while being inspected")
    return sorted(manifest, key=lambda item: str(item["path"]))


def _remove_manifest_subset(root: Path, expected: list[dict[str, Any]]) -> None:
    """Remove an Ava-owned tree, accepting paths deleted by an earlier attempt."""
    expected_by_path = {str(item["path"]): item for item in expected}
    current = _tree_manifest(root)
    for item in current:
        path = str(item["path"])
        wanted = expected_by_path.get(path)
        if wanted is None or item["kind"] != wanted["kind"]:
            raise _ClientConflictError("transaction residue contains an unexpected entry")
        expected_mode = int(wanted["mode"])
        cleanup_mode = expected_mode | stat.S_IRWXU
        allowed_modes = {expected_mode, cleanup_mode}
        if item["kind"] == "directory":
            allowed_modes.add(stat.S_IRWXU)
        if int(item["mode"]) not in allowed_modes:
            raise _ClientConflictError("transaction residue metadata was modified")
        if item["kind"] == "file" and item["sha256"] != wanted["sha256"]:
            raise _ClientConflictError("transaction residue content was modified")

    directories = sorted(
        (item for item in current if item["kind"] == "directory"),
        key=lambda item: str(item["path"]).count("/"),
    )
    for item in directories:
        path = root / str(item["path"])
        current_stat = _lstat(path)
        if not stat.S_ISDIR(current_stat.st_mode):
            raise _ClientConflictError("transaction residue changed during cleanup")
        path.chmod(int(item["mode"]) | stat.S_IRWXU)

    files = (item for item in current if item["kind"] == "file")
    for item in files:
        path = root / str(item["path"])
        data, _ = _read_regular(path, source=False)
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise _ClientConflictError("transaction residue changed during cleanup")
        path.chmod(int(item["mode"]) | stat.S_IRWXU)
        path.unlink()

    for item in reversed(directories):
        if item["path"] == ".":
            continue
        path = root / str(item["path"])
        _lstat(path)
        path.rmdir()
    root.rmdir()


def _write_new(path: Path, data: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
        path.chmod(mode)
    finally:
        if fd != -1:
            os.close(fd)


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename a directory only when the destination is absent."""
    if os.name == "nt":
        source.rename(destination)
        return
    library = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        rename_exclusive = 0x00000004
        result = library.renamex_np(source_bytes, destination_bytes, rename_exclusive)
    elif sys.platform.startswith("linux"):
        at_current_working_directory = -100
        rename_no_replace = 1
        try:
            renameat2 = library.renameat2
        except AttributeError as exc:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable") from exc
        result = renameat2(
            at_current_working_directory,
            source_bytes,
            at_current_working_directory,
            destination_bytes,
            rename_no_replace,
        )
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _copy_source_contents(source: Path, destination: Path) -> None:
    def copy_directory(source_dir: Path, destination_dir: Path) -> None:
        source_stat = _source_lstat(source_dir)
        if not stat.S_ISDIR(source_stat.st_mode):
            raise _SourceIntegrityError("operator skill source directory is invalid")
        for source_child in sorted(source_dir.iterdir(), key=lambda item: item.name):
            if source_dir == source and source_child.name == _MARKER_NAME:
                raise _SourceIntegrityError("operator skill source reserves its ownership marker")
            current = _source_lstat(source_child)
            destination_child = destination_dir / source_child.name
            if stat.S_ISDIR(current.st_mode):
                # Keep the directory writable until its complete contents are
                # present, then apply the source mode as the final metadata.
                destination_child.mkdir(mode=0o700)
                copy_directory(source_child, destination_child)
                destination_child.chmod(stat.S_IMODE(current.st_mode))
            elif stat.S_ISREG(current.st_mode):
                data, mode = _read_regular(source_child, source=True)
                _write_new(destination_child, data, mode)
            else:
                raise _SourceIntegrityError("operator skill source contains an unsupported entry")
        if _signature(_source_lstat(source_dir)) != _signature(source_stat):
            raise _SourceIntegrityError("operator skill source changed while being copied")

    copy_directory(source, destination)
    destination.chmod(stat.S_IMODE(_source_lstat(source).st_mode))
