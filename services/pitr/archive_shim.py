#!/usr/bin/env python3
"""Stable, stdlib-only PostgreSQL archive_command local-spool shim."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

_ARCHIVE_NAME = re.compile(
    r"^(?:[0-9A-F]{24}|[0-9A-F]{8}\.history|[0-9A-F]{24}\.[0-9A-F]{8}\.backup)$"
)
EXIT_USAGE = 2
EXIT_UNSAFE_PATH = 3
EXIT_QUOTA = 4
EXIT_COLLISION = 5
EXIT_IO = 6


def _digest(path: Path) -> bytes:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.digest()


def _validate(source: Path, name: str, spool: Path) -> None:
    if not _ARCHIVE_NAME.fullmatch(name) or Path(name).name != name:
        raise ValueError("unsupported archive filename")
    source_stat = source.lstat()
    if not stat.S_ISREG(source_stat.st_mode) or source.is_symlink() or source.name != name:
        raise ValueError("archive source must be a non-symlink regular file matching %f")
    spool_stat = spool.lstat()
    if not stat.S_ISDIR(spool_stat.st_mode) or spool.is_symlink():
        raise ValueError("spool must be a non-symlink directory")


def _spooled_bytes(spool: Path) -> int:
    total = 0
    for entry in spool.iterdir():
        if entry.name.startswith(".") or entry.name.endswith(".ack"):
            continue
        info = entry.lstat()
        if stat.S_ISREG(info.st_mode) and not entry.is_symlink():
            total += info.st_size
    return total


def archive(source: Path, name: str, spool: Path, hard_bytes: int) -> int:
    """Publish one legal archive file without ever replacing an existing object."""
    try:
        _validate(source, name, spool)
        lock_fd = os.open(spool / ".archive.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            target = spool / name
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    return EXIT_COLLISION
                return 0 if _digest(source) == _digest(target) else EXIT_COLLISION
            size = source.stat().st_size
            if _spooled_bytes(spool) + size > hard_bytes:
                return EXIT_QUOTA
            fd, raw_partial = tempfile.mkstemp(prefix=f".{name}.", suffix=".partial", dir=spool)
            partial = Path(raw_partial)
            try:
                with os.fdopen(fd, "wb") as output, source.open("rb") as input_stream:
                    shutil.copyfileobj(input_stream, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                if _digest(source) != _digest(partial):
                    return EXIT_IO
                try:
                    os.link(partial, target, follow_symlinks=False)
                except FileExistsError:
                    if target.is_symlink() or not target.is_file():
                        return EXIT_COLLISION
                    return 0 if _digest(source) == _digest(target) else EXIT_COLLISION
                directory_fd = os.open(spool, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                return 0
            finally:
                partial.unlink(missing_ok=True)
        finally:
            os.close(lock_fd)
    except ValueError:
        return EXIT_UNSAFE_PATH
    except OSError:
        return EXIT_IO


def self_check() -> int:
    """Prove the copied shim can execute without the checkout or virtualenv."""
    return 0 if _ARCHIVE_NAME.fullmatch("000000010000000000000001") else EXIT_IO


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", nargs="?")
    parser.add_argument("name", nargs="?")
    parser.add_argument("--spool", type=Path)
    parser.add_argument("--hard-bytes", type=int)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        return self_check()
    if args.source is None or args.name is None or args.spool is None or args.hard_bytes is None:
        return EXIT_USAGE
    result = archive(Path(args.source), args.name, args.spool, args.hard_bytes)
    if result:
        sys.stderr.write(f"[ava-pitr-archive] spool failed (exit={result})\n")
    return result


if __name__ == "__main__":
    sys.exit(main())
