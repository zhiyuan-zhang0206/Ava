"""Fail-closed local restore_command used only by an isolated PITR drill."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TypedDict, cast


class _ArchiveRecord(TypedDict):
    path: str
    sha256: str
    size: int


def _digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def restore(mapping_path: Path, archive_name: str, destination: Path) -> None:
    if Path(archive_name).name != archive_name or not archive_name:
        raise ValueError("restore request is not an archive basename")
    mapping = json.loads(mapping_path.read_text())
    if not isinstance(mapping, dict) or archive_name not in mapping:
        raise ValueError("restore request is absent from the protected allowlist")
    record = mapping[archive_name]
    if not isinstance(record, dict):
        raise TypeError("restore allowlist record is not an object")
    if set(record) != {"path", "sha256", "size"}:
        raise ValueError("restore allowlist record does not match schema")
    record = cast(_ArchiveRecord, record)
    source = Path(record["path"])
    if source.is_symlink() or not source.is_file():
        raise ValueError("restore source is not an owned regular file")
    if _digest(source) != (int(record["size"]), str(record["sha256"])):
        raise ValueError("restore source differs from the protected allowlist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    staged = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.link(staged, destination, follow_symlinks=False)
        parent_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        staged.unlink(missing_ok=True)


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(2)
    try:
        restore(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
