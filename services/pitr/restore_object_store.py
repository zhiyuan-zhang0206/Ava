"""Viewer-only, generation-pinned object reads for restore drills."""

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, Protocol, cast

import google_crc32c
from google.api_core.exceptions import (
    BadRequest,
    DeadlineExceeded,
    Forbidden,
    GatewayTimeout,
    InternalServerError,
    NotFound,
    PreconditionFailed,
    ServiceUnavailable,
    TooManyRequests,
    Unauthorized,
)
from google.cloud import storage
from google.cloud.storage.retry import DEFAULT_RETRY_IF_GENERATION_SPECIFIED
from google.oauth2 import service_account

from services.pitr.object_store import PermanentObjectStoreError, TransientObjectStoreError
from services.pitr.restore_manifest import RestoreObject


class GenerationPinnedObjectReader(Protocol):
    def download_exact(self, expected: RestoreObject, destination: Path) -> None: ...


class _ReadableBlob(Protocol):
    name: str
    generation: int | str | None
    size: int | str | None
    crc32c: str | None
    metadata: Mapping[str, str] | None

    def download_to_file(self, file_obj: BinaryIO, **kwargs: object) -> None: ...


class _ReadableBucket(Protocol):
    def get_blob(self, name: str, **kwargs: object) -> _ReadableBlob | None: ...


_TRANSIENT = (
    DeadlineExceeded,
    GatewayTimeout,
    InternalServerError,
    ServiceUnavailable,
    TooManyRequests,
)
_PERMANENT = (BadRequest, Forbidden, Unauthorized, NotFound, PreconditionFailed)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class GCSGenerationPinnedObjectReader:
    """Read one immutable generation; this boundary exposes no write/delete verb."""

    def __init__(
        self, *, project: str, bucket: str, credentials_file: Path, timeout_seconds: int = 300
    ) -> None:
        credentials = service_account.Credentials.from_service_account_file(str(credentials_file))
        self._bucket = cast(
            _ReadableBucket,
            storage.Client(project=project, credentials=credentials).bucket(bucket),
        )
        self._timeout = timeout_seconds

    @classmethod
    def from_bucket_client(
        cls, bucket: _ReadableBucket, *, timeout_seconds: int = 300
    ) -> GCSGenerationPinnedObjectReader:
        instance = cls.__new__(cls)
        instance._bucket = bucket
        instance._timeout = timeout_seconds
        return instance

    def download_exact(self, expected: RestoreObject, destination: Path) -> None:
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("restore download destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = destination.parent / f".{destination.name}.partial"
        if partial.exists() or partial.is_symlink():
            raise FileExistsError("restore download partial already exists")
        owned_partial = False
        try:
            blob = self._bucket.get_blob(
                expected.object_name,
                generation=expected.generation,
                retry=DEFAULT_RETRY_IF_GENERATION_SPECIFIED,
                timeout=self._timeout,
            )
            if blob is None:
                raise PermanentObjectStoreError("pinned restore object does not exist")
            self._verify_properties(blob, expected)
            checksum = google_crc32c.Checksum()  # pyright: ignore[reportUnknownMemberType]
            size = 0
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            owned_partial = True
            with os.fdopen(fd, "wb") as output:
                sink = _ChecksummedWriter(output, checksum)
                blob.download_to_file(
                    cast(BinaryIO, sink),
                    if_generation_match=expected.generation,
                    checksum="crc32c",
                    retry=DEFAULT_RETRY_IF_GENERATION_SPECIFIED,
                    timeout=self._timeout,
                )
                size = sink.size
                output.flush()
                os.fsync(output.fileno())
            crc32c = base64.b64encode(checksum.digest()).decode("ascii")
            if size != expected.size or crc32c != expected.crc32c:
                raise PermanentObjectStoreError("pinned restore object content differs")
            os.link(partial, destination, follow_symlinks=False)
            _fsync_dir(destination.parent)
            partial.unlink()
            _fsync_dir(destination.parent)
            owned_partial = False
        except _TRANSIENT as exc:
            raise TransientObjectStoreError("pinned restore download temporarily failed") from exc
        except _PERMANENT as exc:
            raise PermanentObjectStoreError("pinned restore download was rejected") from exc
        finally:
            if owned_partial:
                partial.unlink(missing_ok=True)

    @staticmethod
    def _verify_properties(blob: _ReadableBlob, expected: RestoreObject) -> None:
        if (
            blob.name != expected.object_name
            or blob.generation is None
            or int(blob.generation) != expected.generation
            or blob.size is None
            or int(blob.size) != expected.size
            or blob.crc32c != expected.crc32c
            or dict(blob.metadata or {}) != dict(expected.metadata)
        ):
            raise PermanentObjectStoreError("pinned restore object properties differ")


class _ChecksummedWriter:
    def __init__(self, output: BinaryIO, checksum: object) -> None:
        self._output = output
        self._checksum = checksum
        self.size = 0

    def write(self, value: bytes) -> int:
        self._checksum.update(value)  # type: ignore[attr-defined]
        written = self._output.write(value)
        if written != len(value):
            raise OSError("short write while downloading restore object")
        self.size += written
        return written

    def flush(self) -> None:
        self._output.flush()
