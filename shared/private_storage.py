"""Owner-only local storage helpers for secrets and uploaded user files."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path


def _private_path_error(path: Path, reason: str) -> RuntimeError:
    return RuntimeError(f"private storage path {path} {reason}")


def ensure_private_dir(path: Path) -> Path:
    """Create `path` if needed, then require an owner-only real directory."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    else:
        if stat.S_ISLNK(before.st_mode):
            raise _private_path_error(path, "is a symlink")
        if not stat.S_ISDIR(before.st_mode):
            raise _private_path_error(path, "is not a directory")

    current = path.lstat()
    if stat.S_ISLNK(current.st_mode):
        raise _private_path_error(path, "is a symlink")
    if not stat.S_ISDIR(current.st_mode):
        raise _private_path_error(path, "is not a directory")
    if os.name != "nt" and current.st_uid != os.geteuid():
        raise _private_path_error(path, "is not owned by the current user")
    path.chmod(0o700)
    return path


def ensure_private_file(path: Path) -> None:
    """Repair an existing regular file to owner-only mode; ignore a missing file."""
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(current.st_mode):
        raise _private_path_error(path, "is a symlink")
    if not stat.S_ISREG(current.st_mode):
        raise _private_path_error(path, "is not a regular file")
    path.chmod(0o600)


def write_private_bytes(path: Path, data: bytes) -> None:
    """Atomically replace `path` with owner-only `data` in its private directory."""
    ensure_private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.name == "nt":
            temporary.chmod(0o600)
        else:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as file:
            fd = -1
            file.write(data)
        os.replace(temporary, path)  # noqa: PTH105 — explicit atomic replacement primitive
        ensure_private_file(path)
    finally:
        if fd != -1:
            os.close(fd)
        temporary.unlink(missing_ok=True)
