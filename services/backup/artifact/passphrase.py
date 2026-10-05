"""The logical-backup passphrase: one resolution for every writer and reader.

Every pg-backup artifact is encrypted (`openssl enc -aes-256-cbc -pbkdf2`)
under one passphrase, the one PINNED at
`$AVA_HOME/backups/logical-backup.passphrase` (0600). It is independent of the
cluster secret (docs/decisions/2026-09-28-backup-passphrase-minted-at-birth.md):

- a gateway home's birth mints a random one (`ensure_minted`), whatever its
  secret, so an empty-secret single box encrypts for real;
- a home born before that carries the passphrase it had encrypted under,
  `sha256(secret)`, pinned once at its conversion; an empty-secret home
  carries a minted one instead, since `sha256("")` is a public constant
  (`LEGACY_EMPTY_SECRET_PASSPHRASE`, which only an explicit restore option
  ever uses);
- rotating the secret never touches it.

Writers and readers never derive a passphrase: a home without a pin has no
logical-backup key and refuses. The pinned file is backup-critical material:
losing it makes every logical backup unreadable, so it belongs with the
gateway's other backup-key copies.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import tempfile
from contextlib import suppress
from pathlib import Path

from base.deploy.release.verified_file import regular_bytes
from base.host.private_storage import private_file_problem, write_private_bytes

PIN_NAME = "logical-backup.passphrase"
_PASSPHRASE = re.compile(r"^[0-9a-f]{64}$")


class PassphrasePinError(RuntimeError):
    """The pinned passphrase is missing, unreadable, malformed, or contradicts a pin request."""


def pin_path(home: Path) -> Path:
    return home / "backups" / PIN_NAME


def derive(cluster_secret: str) -> str:
    """The passphrase a home born before minted passphrases derived from its secret."""
    return hashlib.sha256(cluster_secret.encode()).hexdigest()


# What an empty-secret home encrypted under before it pinned a minted passphrase:
# a public constant, so those artifacts were never confidential. Only an
# explicit restore option (`--legacy-empty-secret-passphrase`) decrypts with it.
LEGACY_EMPTY_SECRET_PASSPHRASE = derive("")


def mint() -> str:
    """A fresh random passphrase (256 bits), in the pinned file's format."""
    return secrets.token_hex(32)


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


def resolve(home: Path) -> str:
    """The passphrase every artifact of `home` is encrypted under; never derived."""
    value = pinned(home)
    if value is None:
        raise PassphrasePinError(
            f"no logical-backup passphrase is pinned at {pin_path(home)}: a gateway home "
            "mints one at birth"
        )
    return value


def pin(home: Path, passphrase: str) -> None:
    """Pin `passphrase` once; an existing pin must be exactly it (never replaced).

    The caller serializes pinning for `home` (birth under the start lock, or a
    rotation under its journal).
    """
    if _PASSPHRASE.fullmatch(passphrase) is None:
        raise PassphrasePinError("a logical-backup passphrase is 64 lowercase hex characters")
    current = pinned(home)
    if current is None:
        write_private_bytes(pin_path(home), (passphrase + "\n").encode())
        current = pinned(home)
    if current != passphrase:
        raise PassphrasePinError(f"{pin_path(home)} pins another passphrase")


def ensure_minted(home: Path) -> None:
    """Birth: pin a freshly minted passphrase unless one is already pinned.

    An interrupted birth that already pinned keeps its passphrase.
    """
    if pinned(home) is None:
        pin(home, mint())


def logical_backup_passphrase() -> str:
    """This home's logical-backup passphrase (see the module docstring)."""
    from base.paths import ava_home

    return resolve(ava_home())


LEGACY_RESTORE_HINT = (
    "an artifact an empty-secret home wrote before its cutover pinned a minted passphrase "
    "decrypts only with the explicit --legacy-empty-secret-passphrase of scripts/data_plane_ops/restore_drill.py"
)


def write_key_file(directory: Path, *, legacy_empty_secret: bool = False) -> Path:
    """Write the logical-backup passphrase to a new private temporary key file.

    The pinned passphrase, or `LEGACY_EMPTY_SECRET_PASSPHRASE` when the caller
    explicitly asks for it (never as a fallback). The key never reaches argv;
    the caller removes the file.
    """
    value = LEGACY_EMPTY_SECRET_PASSPHRASE if legacy_empty_secret else logical_backup_passphrase()
    fd, name = tempfile.mkstemp(prefix=".backup-key-", dir=directory)
    path = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as key_file:
            key_file.write(value)
    except BaseException:
        with suppress(OSError):
            path.unlink(missing_ok=True)
        raise
    return path
