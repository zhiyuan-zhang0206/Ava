"""Streaming AVAPITR1 authenticated encryption."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"AVAPITR1"
TAG_BYTES = 16


@dataclass(frozen=True)
class EncryptedArchive:
    source_sha256: str
    source_size: int
    ciphertext_size: int


@dataclass(frozen=True)
class EncryptionPlan:
    archive_name: str
    key_id: str
    nonce: str
    object_name: str
    source_sha256: str
    source_size: int

    @property
    def header(self) -> bytes:
        return json.dumps(
            {
                "archive_name": self.archive_name,
                "key_id": self.key_id,
                "nonce": self.nonce,
                "object_name": self.object_name,
                "source_sha256": self.source_sha256,
                "source_size": self.source_size,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    @property
    def ciphertext_size(self) -> int:
        return len(MAGIC) + 4 + len(self.header) + self.source_size + TAG_BYTES


def create_plan(source: Path, *, key_id: str, object_name: str) -> EncryptionPlan:
    source_hash, source_size = _source_identity(source)
    return EncryptionPlan(
        archive_name=source.name,
        key_id=key_id,
        nonce=base64.b64encode(os.urandom(12)).decode("ascii"),
        object_name=object_name,
        source_sha256=source_hash,
        source_size=source_size,
    )


class _EncryptedReader(io.RawIOBase):
    def __init__(self, source: Path, key: bytes, plan: EncryptionPlan) -> None:
        self._source = source.open("rb")
        header = plan.header
        self._prefix = MAGIC + struct.pack(">I", len(header)) + header
        self._pending = bytearray(self._prefix)
        self._done = False
        self._encryptor = Cipher(
            algorithms.AES(key), modes.GCM(base64.b64decode(plan.nonce))
        ).encryptor()
        self._encryptor.authenticate_additional_data(self._prefix)

    def readable(self) -> bool:
        return True

    def readinto(self, target: memoryview) -> int | None:
        if not self._pending and not self._done:
            chunk = self._source.read(max(len(target), 1024 * 1024))
            if chunk:
                self._pending.extend(self._encryptor.update(chunk))
            else:
                self._pending.extend(self._encryptor.finalize())
                self._pending.extend(self._encryptor.tag)
                self._done = True
        count = min(len(target), len(self._pending))
        target[:count] = self._pending[:count]
        del self._pending[:count]
        return count

    def close(self) -> None:
        self._source.close()
        super().close()


def open_encrypted(source: Path, *, key: bytes, plan: EncryptionPlan) -> BinaryIO:
    if len(key) != 32:
        raise ValueError("PITR backup key must contain exactly 32 bytes")
    source_hash, source_size = _source_identity(source)
    if (
        source.name != plan.archive_name
        or source_hash != plan.source_sha256
        or source_size != plan.source_size
    ):
        raise ValueError("encryption plan does not describe the source archive")
    return io.BufferedReader(_EncryptedReader(source, key, plan))


def _source_identity(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def encrypt_archive(
    source: Path, destination: Path, *, key: bytes, key_id: str, object_name: str
) -> EncryptedArchive:
    """Encrypt without loading a WAL segment or key into argv/log output."""
    if len(key) != 32:
        raise ValueError("PITR backup key must contain exactly 32 bytes")
    source_hash, source_size = _source_identity(source)
    nonce = os.urandom(12)
    header = json.dumps(
        {
            "archive_name": source.name,
            "key_id": key_id,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "object_name": object_name,
            "source_sha256": source_hash,
            "source_size": source_size,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    prefix = MAGIC + struct.pack(">I", len(header)) + header
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(prefix)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_stream:
            output.write(prefix)
            for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                output.write(encryptor.update(chunk))
            output.write(encryptor.finalize())
            output.write(encryptor.tag)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return EncryptedArchive(source_hash, source_size, destination.stat().st_size)


def decrypt_archive(source: Path, destination: Path, *, key: bytes) -> None:
    """Stream-decrypt and verify source identity (restore/drill seam)."""
    with source.open("rb") as encrypted:
        magic = encrypted.read(len(MAGIC))
        if magic != MAGIC:
            raise ValueError("unsupported PITR encryption format")
        raw_length = encrypted.read(4)
        if len(raw_length) != 4:
            raise ValueError("truncated PITR header")
        header_length = struct.unpack(">I", raw_length)[0]
        header_bytes = encrypted.read(header_length)
        header = json.loads(header_bytes)
        prefix = magic + raw_length + header_bytes
        ciphertext_start = encrypted.tell()
        encrypted.seek(-TAG_BYTES, os.SEEK_END)
        ciphertext_end = encrypted.tell()
        tag = encrypted.read(TAG_BYTES)
        encrypted.seek(ciphertext_start)
        decryptor = Cipher(
            algorithms.AES(key), modes.GCM(base64.b64decode(header["nonce"]), tag)
        ).decryptor()
        decryptor.authenticate_additional_data(prefix)
        digest = hashlib.sha256()
        size = 0
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                remaining = ciphertext_end - ciphertext_start
                while remaining:
                    chunk = encrypted.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("truncated PITR ciphertext")  # noqa: TRY301
                    remaining -= len(chunk)
                    plaintext = decryptor.update(chunk)
                    digest.update(plaintext)
                    size += len(plaintext)
                    output.write(plaintext)
                tail = decryptor.finalize()
                digest.update(tail)
                size += len(tail)
                output.write(tail)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    if size != header["source_size"] or digest.hexdigest() != header["source_sha256"]:
        destination.unlink(missing_ok=True)
        raise ValueError("PITR plaintext identity mismatch")
