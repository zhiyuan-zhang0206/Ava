"""No-follow filesystem primitives for the external operator skill bridge."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MARKER_NAME = ".ava-managed.json"
_POSIX = os.name != "nt"


def _platform_mode(mode: int, *, is_dir: bool) -> int:
    """Normalize a filesystem mode for cross-platform manifest comparison.

    NTFS does not store POSIX permission bits; lstat reports synthetic modes
    (0o666 writable files / 0o777 writable dirs) that never match the mode
    requested at creation.  The bridge's manifest mode field is a POSIX
    security-audit signal, so on non-POSIX platforms it is normalized to the
    pipeline's canonical private modes (0o600 / 0o700).  POSIX is authoritative
    and passed through unchanged.
    """
    if _POSIX:
        return mode
    return 0o700 if is_dir else 0o600


class _SourceIntegrityError(RuntimeError):
    """The repository source cannot safely be copied."""


class _ClientConflictError(RuntimeError):
    """A user-owned client path cannot safely be changed."""


@dataclass(frozen=True, slots=True)
class _SourceFile:
    path: str
    mode: int
    data: bytes


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    """One immutable read of the operator skill source tree."""

    directories: tuple[tuple[str, int], ...]
    files: tuple[_SourceFile, ...]

    def manifest(self) -> list[dict[str, Any]]:
        return sorted(
            [
                *(
                    {"kind": "directory", "mode": mode, "path": path}
                    for path, mode in self.directories
                ),
                *(
                    {
                        "kind": "file",
                        "mode": item.mode,
                        "path": item.path,
                        "sha256": hashlib.sha256(item.data).hexdigest(),
                    }
                    for item in self.files
                ),
            ],
            key=lambda item: str(item["path"]),
        )


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


def _signature(current: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        current.st_dev,
        current.st_ino,
        current.st_mode,
        current.st_nlink,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    )


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare only the identity fields shared by pathname and handle stats."""
    # CPython 3.12's Windows pathname stat keeps legacy st_ctime = birthtime,
    # while fstat reports the handle's metadata-change time.  Compare the two
    # API families only on identity; each family gets its own full before/after
    # stability check in _read_regular.
    left_identity = (left.st_dev, left.st_ino)
    right_identity = (right.st_dev, right.st_ino)
    return left_identity != (0, 0) and left_identity == right_identity


def _read_regular(path: Path, *, source: bool) -> tuple[bytes, int]:
    inspect = _source_lstat if source else _lstat
    before = inspect(path)
    error = _SourceIntegrityError if source else _ClientConflictError
    if not stat.S_ISREG(before.st_mode):
        raise error("operator skill tree contains a non-regular file")
    if before.st_nlink != 1:
        raise error("operator skill tree contains a multi-link file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            opened_before = os.fstat(stream.fileno())
            if not _same_identity(opened_before, before):
                raise error("operator skill tree changed while being read")
            if opened_before.st_nlink not in {0, 1}:
                raise error("operator skill tree contains a multi-link file")
            data = stream.read()
            opened_after = os.fstat(stream.fileno())
            if _signature(opened_after) != _signature(opened_before):
                raise error("operator skill tree changed while being read")
    except OSError as exc:
        raise error("operator skill tree cannot be read safely") from exc
    after = inspect(path)
    if _signature(after) != _signature(before) or not _same_identity(after, opened_after):
        raise error("operator skill tree changed while being read")
    return data, _platform_mode(stat.S_IMODE(before.st_mode), is_dir=False)


def _source_snapshot(root: Path) -> _SourceSnapshot:
    """Capture validated source bytes once so publication never re-reads live Git files."""
    root_before = _source_lstat(root)
    if not stat.S_ISDIR(root_before.st_mode):
        raise _SourceIntegrityError("operator skill tree root is not a directory")
    directories: list[tuple[str, int]] = [
        (".", _platform_mode(stat.S_IMODE(root_before.st_mode), is_dir=True))
    ]
    files: list[_SourceFile] = []
    source_stats: dict[str, os.stat_result] = {".": root_before}

    def visit(directory: Path) -> None:
        relative_directory = directory.relative_to(root).as_posix() or "."
        before = _source_lstat(directory)
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise _SourceIntegrityError("operator skill tree cannot be enumerated safely") from exc
        for child in children:
            if directory == root and child.name == _MARKER_NAME:
                raise _SourceIntegrityError("operator skill source reserves its ownership marker")
            current = _source_lstat(child)
            relative = child.relative_to(root).as_posix()
            source_stats[relative] = current
            if stat.S_ISDIR(current.st_mode):
                directories.append(
                    (
                        relative,
                        _platform_mode(stat.S_IMODE(current.st_mode), is_dir=True),
                    )
                )
                visit(child)
            elif stat.S_ISREG(current.st_mode):
                data, mode = _read_regular(child, source=True)
                files.append(_SourceFile(path=relative, mode=mode, data=data))
            else:
                raise _SourceIntegrityError("operator skill source contains an unsupported entry")
        if _signature(_source_lstat(directory)) != _signature(before):
            raise _SourceIntegrityError("operator skill source changed while being copied")
        source_stats[relative_directory] = before

    visit(root)
    for relative, expected in source_stats.items():
        current = _source_lstat(root if relative == "." else root / relative)
        if _signature(current) != _signature(expected):
            raise _SourceIntegrityError("operator skill source changed while being copied")
    return _SourceSnapshot(directories=tuple(directories), files=tuple(files))


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
        {
            "kind": "directory",
            "mode": _platform_mode(stat.S_IMODE(root_before.st_mode), is_dir=True),
            "path": ".",
        }
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
                        "mode": _platform_mode(stat.S_IMODE(current.st_mode), is_dir=True),
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


def _validate_manifest_subset(
    current: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> None:
    expected_by_path = {str(item["path"]): item for item in expected}
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
        if _POSIX and int(item["mode"]) not in allowed_modes:
            raise _ClientConflictError("transaction residue metadata was modified")
        if item["kind"] == "file" and item["sha256"] != wanted["sha256"]:
            raise _ClientConflictError("transaction residue content was modified")


def _verify_cleanup_file(path: Path, expected: dict[str, Any]) -> None:
    data, mode = _read_regular(path, source=False)
    if hashlib.sha256(data).hexdigest() != expected["sha256"] or (
        _POSIX and mode != int(expected["mode"])
    ):
        raise _ClientConflictError("transaction residue file changed during cleanup")


def _remove_manifest_subset(root: Path, expected: list[dict[str, Any]]) -> None:
    """Refuse path-based deletion after validating the caller's preservation candidate."""
    current = _tree_manifest(root)
    _validate_manifest_subset(current, expected)
    for item in current:
        if item["kind"] == "file":
            _verify_cleanup_file(root / str(item["path"]), item)
    raise _ClientConflictError("path-bound deletion is unsupported; residue was preserved")


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


def _materialize_source_snapshot(snapshot: _SourceSnapshot, destination: Path) -> None:
    """Write one captured source generation without consulting the live checkout."""
    for relative, _mode in sorted(
        (item for item in snapshot.directories if item[0] != "."),
        key=lambda item: len(Path(item[0]).parts),
    ):
        (destination / relative).mkdir(mode=0o700)
    for item in snapshot.files:
        _write_new(destination / item.path, item.data, item.mode)
    for relative, mode in sorted(
        snapshot.directories,
        key=lambda item: len(Path(item[0]).parts),
        reverse=True,
    ):
        (destination if relative == "." else destination / relative).chmod(mode)
