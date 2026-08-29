"""Restartable streaming object boundary for physical base backups."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from typing import BinaryIO, Protocol, cast

from google.api_core.exceptions import (
    BadRequest,
    DeadlineExceeded,
    Forbidden,
    GatewayTimeout,
    InternalServerError,
    PreconditionFailed,
    ServiceUnavailable,
    TooManyRequests,
    Unauthorized,
)
from google.cloud import storage
from google.oauth2 import service_account

from services.pitr.object_store import (
    PermanentObjectStoreError,
    RemoteObjectAck,
    TransientObjectStoreError,
)


class RestartableEncryptedSource(Protocol):
    @property
    def ciphertext_size(self) -> int: ...

    @property
    def ciphertext_crc32c(self) -> str: ...

    def iter_chunks(self) -> Iterable[bytes]: ...


class RestartableStreamingObjectStore(Protocol):
    def put_base_if_absent(
        self,
        *,
        source: RestartableEncryptedSource,
        object_name: str,
        metadata: Mapping[str, str],
    ) -> RemoteObjectAck: ...


class _StreamingBlob(Protocol):
    name: str
    generation: int | str | None
    size: int | str | None
    crc32c: str | None
    metadata: Mapping[str, str] | None

    def open(self, mode: str, **kwargs: object) -> AbstractContextManager[BinaryIO]: ...

    def reload(self, **kwargs: object) -> None: ...


class _StreamingBucket(Protocol):
    def blob(self, name: str) -> _StreamingBlob: ...

    def get_blob(self, name: str, **kwargs: object) -> _StreamingBlob | None: ...


_TRANSIENT = (
    DeadlineExceeded,
    GatewayTimeout,
    InternalServerError,
    ServiceUnavailable,
    TooManyRequests,
)
_PERMANENT = (BadRequest, Forbidden, Unauthorized)


class GCSRestartableStreamingObjectStore:
    """Write bounded chunks to GCS; an outer retry reopens deterministic bytes."""

    def __init__(
        self, *, project: str, bucket: str, credentials_file: str, timeout_seconds: int = 300
    ) -> None:
        credentials = service_account.Credentials.from_service_account_file(  # pyright: ignore[reportUnknownMemberType]
            credentials_file
        )
        self._bucket = cast(
            _StreamingBucket,
            storage.Client(  # pyright: ignore[reportUnknownMemberType]
                project=project, credentials=credentials
            ).bucket(bucket),
        )
        self._timeout = timeout_seconds

    @classmethod
    def from_bucket_client(
        cls, bucket: _StreamingBucket, *, timeout_seconds: int = 300
    ) -> GCSRestartableStreamingObjectStore:
        instance = cls.__new__(cls)
        instance._bucket = bucket
        instance._timeout = timeout_seconds
        return instance

    @staticmethod
    def _ack(blob: _StreamingBlob, *, created: bool) -> RemoteObjectAck:
        if blob.generation is None or blob.size is None or blob.crc32c is None:
            raise TransientObjectStoreError("GCS object omitted verification properties")
        return RemoteObjectAck(
            object_name=str(blob.name),
            generation=int(blob.generation),
            size=int(blob.size),
            crc32c=blob.crc32c,
            metadata=dict(blob.metadata or {}),
            created=created,
        )

    def _stat(self, object_name: str) -> RemoteObjectAck | None:
        try:
            blob = self._bucket.get_blob(object_name, timeout=self._timeout)
        except _TRANSIENT as exc:
            raise TransientObjectStoreError("GCS stat temporarily failed") from exc
        except _PERMANENT as exc:
            raise PermanentObjectStoreError("GCS stat was rejected") from exc
        return None if blob is None else self._ack(blob, created=False)

    def put_base_if_absent(
        self,
        *,
        source: RestartableEncryptedSource,
        object_name: str,
        metadata: Mapping[str, str],
    ) -> RemoteObjectAck:
        blob = self._bucket.blob(object_name)
        blob.metadata = dict(metadata)
        try:
            with blob.open(
                "wb",
                chunk_size=8 * 1024 * 1024,
                if_generation_match=0,
                checksum="crc32c",
                retry=None,
                timeout=self._timeout,
            ) as writer:
                for chunk in source.iter_chunks():
                    writer.write(chunk)
            blob.reload(timeout=self._timeout)
            ack = self._ack(blob, created=True)
        except PreconditionFailed:
            existing = self._stat(object_name)
            if existing is None:
                raise TransientObjectStoreError(
                    "GCS precondition raced with a missing base object"
                ) from None
            ack = existing
        except _TRANSIENT as exc:
            raise TransientObjectStoreError("GCS base upload temporarily failed") from exc
        except _PERMANENT as exc:
            raise PermanentObjectStoreError("GCS base upload was rejected") from exc
        if (
            ack.object_name != object_name
            or ack.generation <= 0
            or ack.size != source.ciphertext_size
            or ack.crc32c != source.ciphertext_crc32c
            or dict(ack.metadata) != dict(metadata)
        ):
            raise PermanentObjectStoreError(
                "immutable GCS base object differs from the candidate stream"
            )
        return ack
