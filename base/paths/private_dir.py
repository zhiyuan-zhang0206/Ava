"""Owner-only directories: create one, or refuse a path that cannot be one.

Pure `stat` and `Path` operations that never log, so the `paths` door can use them
below the logger. `base.host.private_storage` builds the file and tree helpers on
the same refusal rules, so a repair and a pre-flight cannot disagree about what is broken.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

__all__ = ["dir_refusal", "ensure_private_dir", "is_foreign_owned", "private_path_error"]


def private_path_error(path: Path, reason: str) -> RuntimeError:
    return RuntimeError(f"private storage path {path} {reason}")


def is_foreign_owned(current: os.stat_result) -> bool:
    """Return whether a path is not owned by this process on POSIX.

    chmod honors the owner (uid) alone, not the group — a uid-ours file
    with a foreign gid (chgrp'd leftover) is still repairable, and skipping
    it would strand a 0o644 file as group-readable.
    """
    return current.st_uid != os.geteuid()


def dir_refusal(current: os.stat_result) -> str | None:
    """Why a private-directory candidate is unusable, or None.

    One definition for the three readers of the rule: the writer
    (`ensure_private_dir`), the converge walk, and the read-only predicates the
    update leg's pre-stop gate consults — so a repair and a pre-flight cannot
    disagree about what is broken.
    """
    if stat.S_ISLNK(current.st_mode):
        return "is a symlink"
    if not stat.S_ISDIR(current.st_mode):
        return "is not a directory"
    if is_foreign_owned(current):
        return "is not owned by the current user"
    return None


def ensure_private_dir(path: Path) -> Path:
    """Create `path` if needed, then require an owner-only real directory."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    else:
        refusal = dir_refusal(before)
        if refusal is not None:
            raise private_path_error(path, refusal)

    current = path.lstat()
    refusal = dir_refusal(current)
    if refusal is not None:
        raise private_path_error(path, refusal)
    path.chmod(0o700)
    return path
