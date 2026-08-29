"""Authenticate and safely unpack one local AVAPITRB1 ciphertext."""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import struct
import tarfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

import zstandard
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from services.pitr.base_stream import BASE_MAGIC, PACKER_VERSION, snapshot_candidate
from services.pitr.restore_manifest import RestoreObject

_TAG_BYTES = 16
_CHUNK_BYTES = 1024 * 1024
_ARCHIVE_OVERHEAD_BYTES = 128 * 1024 * 1024


class BaseRestoreError(RuntimeError):
    pass


def _header(source: BinaryIO) -> tuple[dict[str, Any], bytes, int, int, bytes]:
    magic = source.read(len(BASE_MAGIC))
    if magic != BASE_MAGIC:
        raise BaseRestoreError("unsupported base backup encryption format")
    raw_length = source.read(4)
    if len(raw_length) != 4:
        raise BaseRestoreError("truncated base backup header")
    header_length = struct.unpack(">I", raw_length)[0]
    if header_length <= 0 or header_length > 64 * 1024:
        raise BaseRestoreError("invalid base backup header length")
    header_bytes = source.read(header_length)
    if len(header_bytes) != header_length:
        raise BaseRestoreError("truncated base backup header")
    raw: dict[str, Any] = json.loads(header_bytes)
    expected = {
        "candidate_sha256",
        "candidate_size",
        "key_id",
        "native_manifest_sha256",
        "nonce",
        "object_name",
        "packer_version",
        "schema_version",
    }
    if set(raw) != expected or raw["packer_version"] != PACKER_VERSION:
        raise BaseRestoreError("base backup header fields do not match schema")
    ciphertext_start = source.tell()
    source.seek(-_TAG_BYTES, os.SEEK_END)
    ciphertext_end = source.tell()
    tag = source.read(_TAG_BYTES)
    if ciphertext_end < ciphertext_start or len(tag) != _TAG_BYTES:
        raise BaseRestoreError("truncated base backup ciphertext")
    source.seek(ciphertext_start)
    return raw, magic + raw_length + header_bytes, ciphertext_start, ciphertext_end, tag


def _verify_header(header: dict[str, Any], expected: RestoreObject) -> None:
    metadata = dict(expected.metadata)
    if (
        header["object_name"] != expected.object_name
        or header["key_id"] != metadata["ava-key-id"]
        or header["candidate_sha256"] != metadata["ava-candidate-sha256"]
        or str(header["packer_version"]) != metadata["ava-packer-version"]
        or header["schema_version"] != 1
        or not isinstance(header["candidate_size"], int)
        or header["candidate_size"] <= 0
        or not isinstance(header["native_manifest_sha256"], str)
        or len(header["native_manifest_sha256"]) != 64
        or len(base64.b64decode(header["nonce"], validate=True)) != 12
    ):
        raise BaseRestoreError("base backup header differs from protected evidence")


def _plaintext_chunks(source_path: Path, *, key: bytes, expected: RestoreObject) -> Iterator[bytes]:
    if len(key) != 32:
        raise BaseRestoreError("PITR backup key must contain exactly 32 bytes")
    with source_path.open("rb") as source:
        header, prefix, start, end, tag = _header(source)
        _verify_header(header, expected)
        decryptor = Cipher(
            algorithms.AES(key), modes.GCM(base64.b64decode(header["nonce"]), tag)
        ).decryptor()
        decryptor.authenticate_additional_data(prefix)
        remaining = end - start
        while remaining:
            chunk = source.read(min(_CHUNK_BYTES, remaining))
            if not chunk:
                raise BaseRestoreError("truncated base backup ciphertext")
            remaining -= len(chunk)
            plaintext = decryptor.update(chunk)
            if plaintext:
                yield plaintext
        tail = decryptor.finalize()
        if tail:
            yield tail


def authenticate_base_ciphertext(
    source: Path, *, key: bytes, expected: RestoreObject
) -> dict[str, Any]:
    for _chunk in _plaintext_chunks(source, key=key, expected=expected):
        pass
    with source.open("rb") as stream:
        header, _prefix, _start, _end, _tag = _header(stream)
    return header


def _decompressed_chunks(chunks: Iterator[bytes], *, maximum: int) -> Iterator[bytes]:
    decompressor = zstandard.ZstdDecompressor().decompressobj()
    produced = 0
    for chunk in chunks:
        if decompressor.eof:
            raise BaseRestoreError("base archive contains a trailing compressed stream")
        value = decompressor.decompress(chunk)
        if decompressor.unused_data:
            raise BaseRestoreError("base archive contains trailing compressed bytes")
        produced += len(value)
        if produced > maximum:
            raise BaseRestoreError("base archive expanded beyond its hard stream bound")
        if value:
            yield value
    tail = decompressor.flush()
    produced += len(tail)
    if not decompressor.eof or produced > maximum:
        raise BaseRestoreError("base archive did not end within its hard stream bound")
    if tail:
        yield tail


class _ChunkReader(io.RawIOBase):
    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._pending = bytearray()
        self._done = False

    def readable(self) -> bool:
        return True

    def readinto(self, target: bytearray) -> int:  # pyright: ignore[reportIncompatibleMethodOverride]
        while not self._pending and not self._done:
            try:
                self._pending.extend(next(self._chunks))
            except StopIteration:
                self._done = True
        count = min(len(target), len(self._pending))
        target[:count] = self._pending[:count]
        del self._pending[:count]
        return count


def extract_authenticated_base(
    source: Path,
    destination: Path,
    *,
    key: bytes,
    expected: RestoreObject,
    candidate_sha256: str,
    native_manifest_sha256: str,
    max_extracted_bytes: int,
) -> Path:
    """Use a second local authenticated pass after the discard pass succeeded."""

    if destination.exists() or destination.is_symlink():
        raise FileExistsError("restore extraction destination already exists")
    destination.mkdir(parents=True, mode=0o700)
    data_root = destination / "data"
    try:
        raw = io.BufferedReader(
            _ChunkReader(  # pyright: ignore[reportArgumentType]
                _decompressed_chunks(
                    _plaintext_chunks(source, key=key, expected=expected),
                    maximum=max_extracted_bytes + _ARCHIVE_OVERHEAD_BYTES,
                )
            )
        )
        with tarfile.open(fileobj=raw, mode="r|") as archive:
            seen: set[str] = set()
            declared_bytes = 0
            member_count = 0
            for member in archive:
                relative = _safe_member(member, seen)
                member_count += 1
                declared_bytes += member.size
                _require_member_budget(
                    count=member_count,
                    declared_bytes=declared_bytes,
                    maximum=max_extracted_bytes,
                )
                target = destination / relative
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=False, mode=0o700)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                extracted = archive.extractfile(member)
                extracted = _require_content(extracted)  # pyright: ignore[reportArgumentType]
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as output:
                    shutil.copyfileobj(extracted, output, length=_CHUNK_BYTES)
                    output.flush()
                    os.fsync(output.fileno())
                _require_member_size(target, member.size)
            while raw.read(_CHUNK_BYTES):
                pass
        _validate_extracted_base(data_root, candidate_sha256, native_manifest_sha256)
        return data_root
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _require_content(value: BinaryIO | None) -> BinaryIO:
    if value is None:
        raise BaseRestoreError("base archive omitted file content")
    return value


def _require_member_budget(*, count: int, declared_bytes: int, maximum: int) -> None:
    if count > 2_000_000 or declared_bytes < 0:
        raise BaseRestoreError("base archive exceeds its safe member bound")
    if declared_bytes > maximum:
        raise BaseRestoreError("base archive exceeds its protected size bound")


def _require_member_size(path: Path, expected: int) -> None:
    if path.stat().st_size != expected:
        raise BaseRestoreError("base archive member size differs")


def _validate_extracted_base(
    data_root: Path, candidate_sha256: str, native_manifest_sha256: str
) -> None:
    _entries, digest = snapshot_candidate(data_root)
    if digest != candidate_sha256:
        raise BaseRestoreError("restored base directory differs from candidate")
    manifest = data_root / "backup_manifest"
    if not manifest.is_file() or _sha256(manifest) != native_manifest_sha256:
        raise BaseRestoreError("restored native manifest differs from candidate")


def _safe_member(member: tarfile.TarInfo, seen: set[str]) -> Path:
    path = PurePosixPath(member.name)
    if (
        path.is_absolute()
        or not path.parts
        or path.parts[0] != "data"
        or any(part in {"", ".", ".."} for part in path.parts)
        or member.name in seen
        or not (member.isdir() or member.isfile())
        or member.islnk()
        or member.issym()
    ):
        raise BaseRestoreError("base archive contains an unsafe member")
    seen.add(member.name)
    return Path(*path.parts)


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()
