"""Detect a writable Google Drive synced folder on this host.

Cross-machine file transfer in the cluster goes through a shared Google Drive:
an agent drops a file into its local Drive folder and a peer on another machine
reads it from theirs. The host-convergence check uses this to fail fast when a
machine cannot participate. This is framework plumbing, not the agent SDK.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from shared.platform import IS_LINUX, IS_MACOS

__all__ = ["candidate_drive_dirs", "find_writable_google_drive"]

# Windows drive-letter mounts surface here under WSL (e.g. /mnt/g). Module-level
# so tests can point it at a fixture tree.
_MNT_ROOT = Path("/mnt")


def _macos_candidates(home: Path) -> list[Path]:
    out: list[Path] = []
    cloudstorage = home / "Library" / "CloudStorage"
    if cloudstorage.is_dir():
        for entry in sorted(cloudstorage.iterdir()):
            # The Google Drive mount root is not writable; files live under its
            # `My Drive` synced subfolder, so that is the candidate to probe.
            if entry.name.startswith("GoogleDrive-"):
                out.append(entry / "My Drive")
    legacy = home / "Google Drive"
    if legacy.is_dir():
        my_drive = legacy / "My Drive"
        out.append(my_drive if my_drive.is_dir() else legacy)
    return out


def _linux_candidates(home: Path) -> list[Path]:
    out: list[Path] = [home / "GoogleDrive", home / "gdrive", home / "google-drive"]
    # WSL: Google Drive for Desktop runs on the Windows side and mounts as a
    # drive letter under /mnt (e.g. /mnt/g), bridged as a 9p `drvfs` mount (not
    # FUSE). The drive root is not writable; its `My Drive` synced subfolder is.
    # Any /mnt child that has a `My Drive` folder is a Google Drive mount -- that
    # marker also keeps a plain Windows drive like /mnt/c from matching.
    if _MNT_ROOT.is_dir():
        for entry in sorted(_MNT_ROOT.iterdir()):
            my_drive = entry / "My Drive"
            if my_drive.is_dir():
                out.append(my_drive)
    # FUSE mounts (rclone / google-drive-ocamlfuse) registered in /proc/mounts:
    # there is no standard mount point on native Linux, so discover them by
    # fstype/source.
    proc_mounts = Path("/proc/mounts")
    if proc_mounts.is_file():
        for line in proc_mounts.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            source, mountpoint, fstype = parts[0], parts[1], parts[2]
            tag = f"{source} {fstype}".lower()
            if "fuse" in fstype.lower() and any(
                k in tag for k in ("rclone", "google", "drive", "gdrive")
            ):
                out.append(Path(mountpoint.replace("\\040", " ")))
    return out


def candidate_drive_dirs() -> list[Path]:
    """Likely Google Drive synced folders on this host (existence not checked)."""
    home = Path.home()
    if IS_MACOS:
        return _macos_candidates(home)
    if IS_LINUX:
        return _linux_candidates(home)
    return []


def _is_writable(d: Path) -> bool:
    """True if a temp file round-trips (write + read + delete) inside `d`."""
    if not d.is_dir():
        return False
    probe = d / f".ava_drive_check_{secrets.token_hex(6)}.tmp"
    try:
        probe.write_text("ok", encoding="utf-8")
        ok = probe.read_text(encoding="utf-8") == "ok"
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)
    return ok


def find_writable_google_drive() -> Path | None:
    """Return a Google Drive synced folder this process can write to, or None.

    Writability is verified by an actual write+read+delete round-trip: a Drive
    mount root can exist yet reject writes (only its synced subfolder accepts
    them), so directory existence alone is not enough to confirm participation.
    """
    for d in candidate_drive_dirs():
        if _is_writable(d):
            return d
    return None
