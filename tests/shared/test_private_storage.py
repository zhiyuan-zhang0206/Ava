"""Private local-storage permissions and atomic write guarantees."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from shared import private_storage


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_private_dir_rejects_symlink(tmp_path: Path) -> None:
    """A pre-placed symlink must never become a private storage directory."""
    target = tmp_path / "private"
    destination = tmp_path / "elsewhere"
    target.symlink_to(destination, target_is_directory=True)

    with pytest.raises(RuntimeError, match=rf"{re.escape(str(target))}.*symlink"):
        private_storage.ensure_private_dir(target)
    assert not destination.exists()


def test_private_dir_rejects_foreign_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A directory owned by another account cannot hold this process's secrets."""
    target = tmp_path / "private"
    target.mkdir()
    owner = os.geteuid()
    monkeypatch.setattr(private_storage.os, "geteuid", lambda: owner + 1)

    with pytest.raises(
        RuntimeError, match=rf"{re.escape(str(target))}.*not owned by the current user"
    ):
        private_storage.ensure_private_dir(target)


def test_private_dir_repairs_mode_drift(tmp_path: Path) -> None:
    """A lax directory from an older umask is tightened before reuse."""
    target = tmp_path / "private"
    target.mkdir()
    target.chmod(0o755)

    assert private_storage.ensure_private_dir(target) == target
    assert _mode(target) == 0o700


def test_private_file_repairs_mode_drift(tmp_path: Path) -> None:
    """An existing secret file is tightened without changing its content."""
    target = tmp_path / "secret"
    target.write_bytes(b"secret")
    target.chmod(0o644)

    private_storage.ensure_private_file(target)

    assert target.read_bytes() == b"secret"
    assert _mode(target) == 0o600


def test_private_write_replaces_existing_content_without_permissive_intermediate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A replacement keeps the prior file intact until a private temp file replaces it."""
    target = tmp_path / "secret"
    target.write_bytes(b"old")
    target.chmod(0o644)
    real_replace = private_storage.os.replace
    seen: dict[str, Path] = {}

    def _replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        seen["temporary"] = source_path
        assert source_path.parent == target.parent
        assert _mode(source_path) == 0o600
        assert destination_path.read_bytes() == b"old"
        real_replace(source, destination)

    monkeypatch.setattr(private_storage.os, "replace", _replace)
    private_storage.write_private_bytes(target, b"new")

    assert seen["temporary"] != target
    assert target.read_bytes() == b"new"
    assert _mode(target) == 0o600
