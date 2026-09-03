"""Owner-only local storage helpers for secrets and uploaded user files."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

from shared.log import logger


def _private_path_error(path: Path, reason: str) -> RuntimeError:
    return RuntimeError(f"private storage path {path} {reason}")


def _is_foreign_owned(current: os.stat_result) -> bool:
    """Return whether a path is not owned by this process on POSIX.

    chmod honors the owner (uid) alone, not the group — a uid-ours file
    with a foreign gid (chgrp'd leftover) is still repairable, and skipping
    it would strand a 0o644 file as group-readable.
    """
    return os.name != "nt" and current.st_uid != os.geteuid()


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
    path.chmod(0o700 if current.st_mode & stat.S_IXUSR else 0o600)


def converge_private_tree(path: Path) -> Path:
    """Recursively converge a private directory tree to owner-only modes.

    A unit's logs, workspaces, and memory checkout can predate the private
    storage convention. Converge owns their durable permission repair, but it
    must never follow a symlink out of the tree while doing so.
    """
    if _is_foreign_owned(path.lstat()):
        # A directory owned by another account can never be chmod'd by
        # converge — warn and leave it (and its subtree) alone instead of
        # aborting the whole converge run (wsl 2026-09-02 boot loop).
        logger.warning("private storage convergence skipped foreign-owned path {path}", path=path)
        return path
    ensure_private_dir(path)
    for child in path.iterdir():
        current = child.lstat()
        if stat.S_ISLNK(current.st_mode):
            # Workspace trees can link tooling outside AVA_HOME; private-tree
            # convergence must not recurse into or alter those targets.
            logger.warning("private storage convergence skipped symlink {path}", path=child)
            continue
        if _is_foreign_owned(current):
            logger.warning(
                "private storage convergence skipped foreign-owned path {path}", path=child
            )
            continue
        if stat.S_ISDIR(current.st_mode):
            converge_private_tree(child)
            continue
        if not stat.S_ISREG(current.st_mode):
            raise _private_path_error(child, "is not a regular file or directory")
        ensure_private_file(child)
    return path


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
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)  # noqa: PTH105 — explicit atomic replacement primitive
        ensure_private_file(path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd != -1:
            os.close(fd)
        temporary.unlink(missing_ok=True)
