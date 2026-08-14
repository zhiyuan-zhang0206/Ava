"""shared.google_drive: detect a writable Google Drive synced folder.

`Path.home()` resolves through `$HOME` (CPython); tests patch it to a tmp dir
and patch `gd.sys.platform` so the per-OS candidate logic runs regardless of the
host the suite executes on.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import shared.google_drive as gd

# Permission test no-ops as root (root bypasses fs perms); CI runs as root.
_skip_if_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="root bypasses fs perms — readonly-dir test no-op as root"
)


# ── _is_writable round-trip ──────────────────────────────────────────────


def test_is_writable_true_and_cleans_up(tmp_path: Path) -> None:
    assert gd._is_writable(tmp_path) is True
    # the probe file is removed after the round-trip — nothing left behind
    assert list(tmp_path.glob(".ava_drive_check_*")) == []


def test_is_writable_false_for_missing_dir(tmp_path: Path) -> None:
    assert gd._is_writable(tmp_path / "nope") is False


def test_is_writable_false_for_file_target(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_text("x")
    assert gd._is_writable(f) is False


@_skip_if_root
def test_is_writable_false_for_readonly_dir(tmp_path: Path) -> None:
    d = tmp_path / "ro"
    d.mkdir()
    d.chmod(0o500)
    try:
        assert gd._is_writable(d) is False
    finally:
        d.chmod(0o700)


# ── find_writable_google_drive ───────────────────────────────────────────


def test_find_returns_first_writable_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    good = tmp_path / "good"
    good.mkdir()
    monkeypatch.setattr(gd, "candidate_drive_dirs", lambda: [tmp_path / "missing", good])
    assert gd.find_writable_google_drive() == good


def test_find_none_when_no_candidate_writable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(gd, "candidate_drive_dirs", lambda: [tmp_path / "missing"])
    assert gd.find_writable_google_drive() is None


def test_find_none_when_no_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gd, "candidate_drive_dirs", list)
    assert gd.find_writable_google_drive() is None


# ── candidate_drive_dirs per-OS ──────────────────────────────────────────


def test_macos_candidates_point_at_my_drive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """macOS CloudStorage entry -> its `My Drive` subfolder (mount root is not writable)."""
    mount = tmp_path / "Library" / "CloudStorage" / "GoogleDrive-someone@example.com" / "My Drive"
    mount.mkdir(parents=True)
    monkeypatch.setattr(gd, "IS_MACOS", True)
    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        assert mount in gd.candidate_drive_dirs()


def test_macos_legacy_google_drive_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "Google Drive" / "My Drive").mkdir(parents=True)
    monkeypatch.setattr(gd, "IS_MACOS", True)
    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        assert (tmp_path / "Google Drive" / "My Drive") in gd.candidate_drive_dirs()


def test_linux_candidates_include_fixed_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(gd, "IS_MACOS", False)
    monkeypatch.setattr(gd, "IS_LINUX", True)
    monkeypatch.setattr(gd, "_MNT_ROOT", tmp_path / "no-mnt")  # isolate from the host's real /mnt
    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        cands = gd.candidate_drive_dirs()
    assert (tmp_path / "GoogleDrive") in cands


def test_wsl_drive_letter_mount_matched_by_my_drive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A /mnt/<letter> with a `My Drive` subfolder is a Drive mount (WSL); a
    plain Windows drive without one (e.g. /mnt/c) must not match."""
    mnt = tmp_path / "mnt"
    (mnt / "g" / "My Drive").mkdir(parents=True)
    (mnt / "c").mkdir()  # plain Windows drive, no `My Drive`
    monkeypatch.setattr(gd, "IS_MACOS", False)
    monkeypatch.setattr(gd, "IS_LINUX", True)
    monkeypatch.setattr(gd, "_MNT_ROOT", mnt)
    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        cands = gd.candidate_drive_dirs()
    assert (mnt / "g" / "My Drive") in cands
    assert (mnt / "c") not in cands
    assert (mnt / "c" / "My Drive") not in cands


def test_unsupported_platform_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gd, "IS_MACOS", False)
    monkeypatch.setattr(gd, "IS_LINUX", False)
    assert gd.candidate_drive_dirs() == []
