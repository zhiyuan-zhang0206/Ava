"""Backend-agnostic content-checksum vocabulary and verification dispatch.

GCS verifies objects with CRC32C (base64-encoded, the SDK's canonical
form); Baidu Netdisk only exposes MD5 (hex). The store ACK therefore
carries an ``(algo, value)`` pair and every local comparison routes
through this module, so a backend swap never smuggles one algorithm's
digest into another's comparison.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path

import google_crc32c

CRC32C = "crc32c"
"""Base64-encoded CRC32C — the GCS canonical digest encoding."""

MD5 = "md5"
"""Hex-encoded MD5 — the Baidu Netdisk PCS digest encoding."""

KNOWN_CHECKSUM_ALGOS = frozenset({CRC32C, MD5})


@dataclass(frozen=True)
class ObjectChecksum:
    """One backend-verified content digest on a remote immutable object."""

    algo: str
    value: str

    def __post_init__(self) -> None:
        if self.algo not in KNOWN_CHECKSUM_ALGOS:
            raise ValueError(
                f"unsupported checksum algorithm {self.algo!r} "
                f"(known: {', '.join(sorted(KNOWN_CHECKSUM_ALGOS))})"
            )


def digest_bytes(algo: str, data: bytes) -> str:
    """Compute the canonical digest value of ``data`` under ``algo``."""
    if algo == CRC32C:
        checksum = google_crc32c.Checksum()  # pyright: ignore[reportUnknownMemberType]
        checksum.update(data)  # pyright: ignore[reportUnknownMemberType]
        return base64.b64encode(checksum.digest()).decode("ascii")  # pyright: ignore[reportUnknownMemberType]
    if algo == MD5:
        return hashlib.md5(data).hexdigest()  # noqa: S324
    raise ValueError(f"unsupported checksum algorithm {algo!r}")


def matches(checksum: ObjectChecksum, data: bytes) -> bool:
    """Constant-time comparison of ``data`` against a stored digest."""
    return hmac.compare_digest(digest_bytes(checksum.algo, data), checksum.value)


def digest_file(algo: str, path: str) -> str:
    """Compute the canonical digest of a seekable file's bytes under ``algo``
    (streamed — the file is never loaded whole)."""
    if algo == CRC32C:
        checksum = google_crc32c.Checksum()  # pyright: ignore[reportUnknownMemberType]
        with Path(path).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                checksum.update(chunk)  # pyright: ignore[reportUnknownMemberType]
        return base64.b64encode(checksum.digest()).decode("ascii")  # pyright: ignore[reportUnknownMemberType]
    if algo == MD5:
        digest = hashlib.md5()  # noqa: S324
        with Path(path).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    raise ValueError(f"unsupported checksum algorithm {algo!r}")
