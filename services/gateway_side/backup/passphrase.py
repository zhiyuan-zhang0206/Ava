"""The logical-backup passphrase: one resolution for every writer and reader.

Every pg-backup artifact is encrypted (`openssl enc -aes-256-cbc -pbkdf2`)
under one passphrase. Until the gateway's human cluster secret first rotates,
that passphrase is derived from the secret (`sha256(secret)`, hex). A bearer
rotation (the fleet cutover's `api` step, or the emergency
`scripts/rotate_cluster_secret.py`) first PINS the then-current passphrase to
`$AVA_HOME/backups/logical-backup.passphrase` (0600); from then on the pinned
passphrase is the key for writers and readers alike, so artifacts written
before and after any number of rotations share it.

The pinned file is backup-critical material: losing it together with the
pre-rotation secret makes every logical backup unreadable. It belongs with the
gateway's other backup-key copies.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from shared.private_storage import private_file_problem, write_private_bytes
from shared.verified_file import regular_bytes

PIN_NAME = "logical-backup.passphrase"
_PASSPHRASE = re.compile(r"^[0-9a-f]{64}$")


class PassphrasePinError(RuntimeError):
    """The pinned passphrase is unreadable, malformed, or contradicts a pin request."""


def pin_path(home: Path) -> Path:
    return home / "backups" / PIN_NAME


def derive(cluster_secret: str) -> str:
    """The passphrase a never-rotated home derives from its human secret."""
    return hashlib.sha256(cluster_secret.encode()).hexdigest()


def fingerprint(passphrase: str) -> str:
    """A digest of a passphrase that is safe to journal (it never reveals it)."""
    return hashlib.sha256(passphrase.encode()).hexdigest()


def pinned(home: Path) -> str | None:
    """The pinned passphrase, or None when this home never pinned one."""
    path = pin_path(home)
    if not path.exists():
        return None
    problem = private_file_problem(path)
    if problem is None and os.name != "nt" and path.lstat().st_mode & 0o077:
        problem = "is not owner-only"
    if problem is not None:
        raise PassphrasePinError(f"{path}: {problem}")
    value = regular_bytes(path, max_bytes=4096).decode().strip()
    if _PASSPHRASE.fullmatch(value) is None:
        raise PassphrasePinError(f"{path} does not hold a logical-backup passphrase")
    return value


def resolve(home: Path, cluster_secret: str) -> str:
    """The passphrase every artifact of `home` is encrypted under."""
    value = pinned(home)
    return derive(cluster_secret) if value is None else value


def pin(home: Path, passphrase: str) -> None:
    """Pin `passphrase` once; an existing pin must be exactly it (never replaced).

    The caller holds the rotation's lock and has journaled this intent.
    """
    if _PASSPHRASE.fullmatch(passphrase) is None:
        raise PassphrasePinError("a logical-backup passphrase is 64 lowercase hex characters")
    current = pinned(home)
    if current is None:
        write_private_bytes(pin_path(home), (passphrase + "\n").encode())
        current = pinned(home)
    if current != passphrase:
        raise PassphrasePinError(f"{pin_path(home)} pins another passphrase")


def logical_backup_passphrase() -> str:
    """This home's logical-backup passphrase (see the module docstring)."""
    from shared.config import settings
    from shared.paths import ava_home

    return resolve(ava_home(), settings.data_plane.cluster_secret)
