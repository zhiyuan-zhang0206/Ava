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


def test_private_tree_recursively_repairs_existing_mode_drift(tmp_path: Path) -> None:
    """Converge makes every existing private-tree node owner-only."""
    root = tmp_path / "private"
    nested = root / "agent" / "artifacts"
    nested.mkdir(parents=True)
    payload = nested / "result.txt"
    payload.write_text("secret")
    for directory in (root, root / "agent", nested):
        directory.chmod(0o755)
    payload.chmod(0o644)

    assert private_storage.converge_private_tree(root) == root

    assert _mode(root) == 0o700
    assert _mode(root / "agent") == 0o700
    assert _mode(nested) == 0o700
    assert _mode(payload) == 0o600


def test_private_tree_rejects_nested_symlink(tmp_path: Path) -> None:
    """Converge refuses a link instead of chmodding an outside target."""
    root = tmp_path / "private"
    root.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (root / "link").symlink_to(target, target_is_directory=True)

    with pytest.raises(RuntimeError, match=rf"{re.escape(str(root / 'link'))}.*symlink"):
        private_storage.converge_private_tree(root)


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


def test_private_write_keeps_previous_complete_value_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "secret"
    target.write_bytes(b"old-complete")

    def _fail_replace(_source: object, _destination: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(private_storage.os, "replace", _fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        private_storage.write_private_bytes(target, b"new-complete")

    assert target.read_bytes() == b"old-complete"
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is POSIX-only")
def test_private_write_fsyncs_payload_and_parent_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[int] = []
    real_fsync = private_storage.os.fsync

    def _fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(private_storage.os, "fsync", _fsync)
    private_storage.write_private_bytes(tmp_path / "secret", b"durable")

    assert len(calls) == 2
