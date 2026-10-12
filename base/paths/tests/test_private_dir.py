"""Owner-only directories: refusals and mode repair."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from base.paths import private_dir


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_private_dir_rejects_symlink(tmp_path: Path) -> None:
    """A pre-placed symlink must never become a private storage directory."""
    target = tmp_path / "private"
    destination = tmp_path / "elsewhere"
    target.symlink_to(destination, target_is_directory=True)

    with pytest.raises(RuntimeError, match=rf"{re.escape(str(target))}.*symlink"):
        private_dir.ensure_private_dir(target)
    assert not destination.exists()


def test_private_dir_rejects_foreign_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A directory owned by another account cannot hold this process's secrets."""
    target = tmp_path / "private"
    target.mkdir()
    owner = os.geteuid()
    monkeypatch.setattr(private_dir.os, "geteuid", lambda: owner + 1)

    with pytest.raises(
        RuntimeError, match=rf"{re.escape(str(target))}.*not owned by the current user"
    ):
        private_dir.ensure_private_dir(target)


def test_is_foreign_owned_is_uid_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ownership follows the uid alone; a foreign gid does not make a path foreign."""

    def _stat(uid: int, gid: int) -> os.stat_result:
        return os.stat_result((0o100644, 0, 0, 1, uid, gid, 0, 0, 0, 0))

    ours = _stat(os.geteuid(), os.geteuid() + 1)
    assert private_dir.is_foreign_owned(ours) is False

    theirs = _stat(os.geteuid() + 1, os.geteuid())
    assert private_dir.is_foreign_owned(theirs) is True


def test_private_dir_repairs_mode_drift(tmp_path: Path) -> None:
    """A lax directory from an older umask is tightened before reuse."""
    target = tmp_path / "private"
    target.mkdir()
    target.chmod(0o755)

    assert private_dir.ensure_private_dir(target) == target
    assert _mode(target) == 0o700
