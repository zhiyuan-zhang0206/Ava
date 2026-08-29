"""Canonical tar → zstd → authenticated encryption for base candidates."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import multiprocessing
import os
import queue
import stat
import struct
import tarfile
import tempfile
from collections.abc import Buffer, Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol, cast

import google_crc32c
import zstandard
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BASE_MAGIC = b"AVAPITRB1"
PACKER_VERSION = 1
CHUNK_BYTES = 8 * 1024 * 1024


class _ProcessEvent(Protocol):
    def is_set(self) -> bool: ...

    def set(self) -> None: ...


class _ProcessQueue(Protocol):
    def put(self, item: object, timeout: float | None = None) -> None: ...

    def get(self, timeout: float | None = None) -> object: ...

    def close(self) -> None: ...

    def join_thread(self) -> None: ...


@dataclass(frozen=True)
class CandidateEntry:
    path: str
    kind: str
    size: int
    sha256: str | None


@dataclass(frozen=True)
class BaseEncryptionPlan:
    schema_version: int
    candidate_sha256: str
    candidate_size: int
    native_manifest_sha256: str
    nonce: str
    object_name: str
    key_id: str
    packer_version: int
    ciphertext_size: int
    ciphertext_crc32c: str

    @property
    def header(self) -> bytes:
        return json.dumps(
            {
                "candidate_sha256": self.candidate_sha256,
                "candidate_size": self.candidate_size,
                "key_id": self.key_id,
                "native_manifest_sha256": self.native_manifest_sha256,
                "nonce": self.nonce,
                "object_name": self.object_name,
                "packer_version": self.packer_version,
                "schema_version": self.schema_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()


def snapshot_candidate(root: Path) -> tuple[tuple[CandidateEntry, ...], str]:
    entries: list[CandidateEntry] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
        ):
            raise ValueError(f"base candidate contains unsupported entry: {relative}")
        if stat.S_ISDIR(info.st_mode):
            entries.append(CandidateEntry(relative, "directory", 0, None))
            continue
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
                digest.update(chunk)
        entries.append(CandidateEntry(relative, "file", info.st_size, digest.hexdigest()))
    encoded = json.dumps(
        [asdict(entry) for entry in entries], sort_keys=True, separators=(",", ":")
    ).encode()
    return tuple(entries), hashlib.sha256(encoded).hexdigest()


class _VerifiedReader(io.RawIOBase):
    def __init__(self, path: Path, entry: CandidateEntry, cancelled: _ProcessEvent) -> None:
        self._source = path.open("rb")
        self._entry = entry
        self._cancelled = cancelled
        self._digest = hashlib.sha256()
        self._size = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self._cancelled.is_set():
            raise RuntimeError("base stream consumer stopped")
        value = self._source.read(size)
        if self._cancelled.is_set():
            raise RuntimeError("base stream consumer stopped")
        self._digest.update(value)
        self._size += len(value)
        return value

    def verify(self) -> None:
        if self._size != self._entry.size or self._digest.hexdigest() != self._entry.sha256:
            raise ValueError(f"base candidate changed while reading {self._entry.path}")

    def close(self) -> None:
        self._source.close()
        super().close()


class _EncryptingSink(io.RawIOBase):
    def __init__(
        self,
        key: bytes,
        plan: BaseEncryptionPlan,
        output: _ProcessQueue,
        cancelled: _ProcessEvent,
    ) -> None:
        if len(key) != 32:
            raise ValueError("PITR backup key must contain exactly 32 bytes")
        self._output = output
        self._cancelled = cancelled
        prefix = BASE_MAGIC + struct.pack(">I", len(plan.header)) + plan.header
        self._encryptor = Cipher(
            algorithms.AES(key), modes.GCM(base64.b64decode(plan.nonce))
        ).encryptor()
        self._encryptor.authenticate_additional_data(prefix)
        self._buffer = bytearray(prefix)

    def writable(self) -> bool:
        return True

    def write(self, value: Buffer) -> int:
        raw = bytes(value)
        self._buffer.extend(self._encryptor.update(raw))
        self._flush_chunks()
        return len(raw)

    def _flush_chunks(self) -> None:
        while len(self._buffer) >= CHUNK_BYTES:
            _put(self._output, bytes(self._buffer[:CHUNK_BYTES]), self._cancelled)
            del self._buffer[:CHUNK_BYTES]

    def finish(self) -> None:
        self._buffer.extend(self._encryptor.finalize())
        self._buffer.extend(self._encryptor.tag)
        self._flush_chunks()
        if self._buffer:
            _put(self._output, bytes(self._buffer), self._cancelled)
            self._buffer.clear()


def _put(output: _ProcessQueue, item: object, cancelled: _ProcessEvent) -> None:
    while not cancelled.is_set():
        try:
            output.put(item, timeout=0.1)
            return
        except queue.Full:
            continue
    raise RuntimeError("base stream consumer stopped")


def _tar_info(entry: CandidateEntry) -> tarfile.TarInfo:
    name = PurePosixPath("data") / entry.path
    info = tarfile.TarInfo(name.as_posix())
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    if entry.kind == "directory":
        info.type = tarfile.DIRTYPE
        info.mode = 0o700
    else:
        info.type = tarfile.REGTYPE
        info.mode = 0o600
        info.size = entry.size
    return info


def _produce(
    root: Path,
    entries: tuple[CandidateEntry, ...],
    candidate_sha256: str,
    key: bytes,
    plan: BaseEncryptionPlan,
    output: _ProcessQueue,
    cancelled: _ProcessEvent,
) -> None:
    try:
        sink = _EncryptingSink(key, plan, output, cancelled)
        compressor = zstandard.ZstdCompressor(
            level=1, threads=0, write_checksum=True, write_content_size=False
        )
        with (
            compressor.stream_writer(cast(BinaryIO, sink), closefd=False) as compressed,
            tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive,
        ):
            for entry in entries:
                info = _tar_info(entry)
                if entry.kind == "directory":
                    archive.addfile(info)
                else:
                    with _VerifiedReader(root / entry.path, entry, cancelled) as source:
                        archive.addfile(info, source)
                        source.verify()
        _require_unchanged(root, entries, candidate_sha256)
        sink.finish()
        _put(output, None, cancelled)
    except BaseException as exc:
        if not cancelled.is_set():
            _put(output, exc, cancelled)


def _require_unchanged(
    root: Path, entries: tuple[CandidateEntry, ...], candidate_sha256: str
) -> None:
    final_entries, final_digest = snapshot_candidate(root)
    if final_entries != entries or final_digest != candidate_sha256:
        raise ValueError("base candidate changed while its stream was being produced")


class RestartableBaseSource:
    def __init__(
        self,
        *,
        root: Path,
        entries: tuple[CandidateEntry, ...],
        key: bytes,
        plan: BaseEncryptionPlan,
    ) -> None:
        self._root = root
        self._entries = entries
        self._key = key
        self._plan = plan

    @property
    def ciphertext_size(self) -> int:
        return self._plan.ciphertext_size

    @property
    def ciphertext_crc32c(self) -> str:
        return self._plan.ciphertext_crc32c

    def iter_chunks(self) -> Iterable[bytes]:
        entries, digest = snapshot_candidate(self._root)
        if entries != self._entries or digest != self._plan.candidate_sha256:
            raise ValueError("base candidate changed after its encryption plan was created")
        context = multiprocessing.get_context("spawn")
        output = cast(_ProcessQueue, context.Queue(maxsize=2))
        cancelled = cast(_ProcessEvent, context.Event())
        producer = context.Process(
            target=_produce,
            args=(
                self._root,
                self._entries,
                self._plan.candidate_sha256,
                self._key,
                self._plan,
                output,
                cancelled,
            ),
            daemon=False,
        )
        producer.start()
        try:
            while True:
                try:
                    item = output.get(timeout=1)
                except queue.Empty:
                    if not producer.is_alive():
                        raise RuntimeError("base stream producer exited without a result") from None
                    continue
                if item is None:
                    producer.join()
                    return
                if isinstance(item, BaseException):
                    producer.join()
                    raise item
                if not isinstance(item, bytes):
                    raise TypeError("canonical base stream produced an invalid chunk")
                yield item
        finally:
            cancelled.set()
            producer.join(timeout=5)
            if producer.is_alive():
                producer.terminate()
                producer.join(timeout=5)
            if producer.is_alive():
                producer.kill()
                producer.join(timeout=5)
            output.close()
            output.join_thread()
            if producer.is_alive():
                raise RuntimeError("base stream producer could not be reaped")


def preflight_source(
    root: Path, *, key: bytes, key_id: str, object_name: str
) -> tuple[RestartableBaseSource, BaseEncryptionPlan]:
    entries, candidate_sha = snapshot_candidate(root)
    native_manifest_sha = _native_manifest_sha256(entries)
    provisional = BaseEncryptionPlan(
        schema_version=1,
        candidate_sha256=candidate_sha,
        candidate_size=sum(entry.size for entry in entries),
        native_manifest_sha256=native_manifest_sha,
        nonce=base64.b64encode(os.urandom(12)).decode("ascii"),
        object_name=object_name,
        key_id=key_id,
        packer_version=PACKER_VERSION,
        ciphertext_size=0,
        ciphertext_crc32c="",
    )
    source = RestartableBaseSource(root=root, entries=entries, key=key, plan=provisional)
    checksum = google_crc32c.Checksum()  # pyright: ignore[reportUnknownMemberType]
    size = 0
    for chunk in source.iter_chunks():
        checksum.update(chunk)  # pyright: ignore[reportUnknownMemberType]
        size += len(chunk)
    plan = replace(
        provisional,
        ciphertext_size=size,
        ciphertext_crc32c=base64.b64encode(checksum.digest()).decode("ascii"),
    )
    return RestartableBaseSource(root=root, entries=entries, key=key, plan=plan), plan


def load_or_create_source(
    root: Path,
    *,
    plan_path: Path,
    key: bytes,
    key_id: str,
    object_name: str,
) -> tuple[RestartableBaseSource, BaseEncryptionPlan]:
    entries, candidate_sha = snapshot_candidate(root)
    candidate_size = sum(entry.size for entry in entries)
    native_manifest_sha = _native_manifest_sha256(entries)
    if plan_path.exists():
        try:
            plan = BaseEncryptionPlan(**json.loads(plan_path.read_text()))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid durable base encryption plan") from exc
        if (
            plan.schema_version != 1
            or plan.candidate_sha256 != candidate_sha
            or plan.candidate_size != candidate_size
            or plan.native_manifest_sha256 != native_manifest_sha
            or plan.object_name != object_name
            or plan.key_id != key_id
            or plan.packer_version != PACKER_VERSION
            or plan.ciphertext_size <= 0
            or not plan.ciphertext_crc32c
            or not _valid_encoded_bytes(plan.nonce, expected_size=12)
            or not _valid_encoded_bytes(plan.ciphertext_crc32c, expected_size=4)
        ):
            raise ValueError("durable base encryption plan does not match its candidate")
        source = RestartableBaseSource(root=root, entries=entries, key=key, plan=plan)
        _verify_planned_stream(source, plan)
        return source, plan
    source, plan = preflight_source(root, key=key, key_id=key_id, object_name=object_name)
    plan_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=f".{plan_path.name}.", dir=plan_path.parent)
    staged = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(asdict(plan), output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        staged.replace(plan_path)
        dir_fd = os.open(plan_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        staged.unlink(missing_ok=True)
    return source, plan


def _native_manifest_sha256(entries: tuple[CandidateEntry, ...]) -> str:
    native_manifests = [
        entry for entry in entries if entry.path == "backup_manifest" and entry.kind == "file"
    ]
    if len(native_manifests) != 1 or native_manifests[0].sha256 is None:
        raise ValueError("base candidate must contain exactly one regular backup_manifest")
    return native_manifests[0].sha256


def _valid_encoded_bytes(value: str, *, expected_size: int) -> bool:
    try:
        return len(base64.b64decode(value, validate=True)) == expected_size
    except (ValueError, TypeError):
        return False


def _verify_planned_stream(source: RestartableBaseSource, plan: BaseEncryptionPlan) -> None:
    checksum = google_crc32c.Checksum()  # pyright: ignore[reportUnknownMemberType]
    size = 0
    for chunk in source.iter_chunks():
        checksum.update(chunk)  # pyright: ignore[reportUnknownMemberType]
        size += len(chunk)
    crc = base64.b64encode(checksum.digest()).decode("ascii")
    if size != plan.ciphertext_size or crc != plan.ciphertext_crc32c:
        raise ValueError("durable base encryption plan does not reproduce its ciphertext")
