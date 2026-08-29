"""Google Cloud Storage adapter for the owned immutable-object boundary."""

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

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
from google.cloud.storage.retry import DEFAULT_RETRY, DEFAULT_RETRY_IF_GENERATION_SPECIFIED
from google.oauth2 import service_account

from services.pitr.object_store import (
    PermanentObjectStoreError,
    RemoteObjectAck,
    TransientObjectStoreError,
)

_TRANSIENT = (
    DeadlineExceeded,
    GatewayTimeout,
    InternalServerError,
    ServiceUnavailable,
    TooManyRequests,
)
_PERMANENT = (BadRequest, Forbidden, Unauthorized)


class BlobClient(Protocol):
    name: str
    generation: int | str | None
    size: int | str | None
    crc32c: str | None
    metadata: Mapping[str, str] | None

    def upload_from_filename(self, filename: str, **kwargs: object) -> None: ...

    def reload(self, **kwargs: object) -> None: ...


class BucketClient(Protocol):
    def blob(self, name: str) -> BlobClient: ...

    def get_blob(self, name: str, **kwargs: object) -> BlobClient | None: ...


class GCSObjectStore:
    """One bucket-scoped adapter; credentials never enter process argv."""

    def __init__(
        self, *, project: str, bucket: str, credentials_file: Path, timeout_seconds: int = 30
    ) -> None:
        # The google-cloud-storage stubs leave Blob members Unknown; the
        # adapter narrows at this boundary so the typed ObjectStore contract
        # above stays the strong interface.
        credentials = service_account.Credentials.from_service_account_file(  # pyright: ignore[reportUnknownMemberType]
            str(credentials_file)
        )
        self._bucket = cast(
            BucketClient,
            storage.Client(  # pyright: ignore[reportUnknownMemberType]
                project=project, credentials=credentials
            ).bucket(bucket),
        )
        self._timeout = timeout_seconds

    @staticmethod
    def _ack(blob: BlobClient, *, created: bool) -> RemoteObjectAck:
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

    @classmethod
    def from_bucket_client(
        cls, bucket: BucketClient, *, timeout_seconds: int = 30
    ) -> GCSObjectStore:
        """Construct around a transport-controlled SDK bucket for contract tests."""

        instance = cls.__new__(cls)
        instance._bucket = bucket
        instance._timeout = timeout_seconds
        return instance

    def stat(self, object_name: str) -> RemoteObjectAck | None:
        try:
            blob = self._bucket.get_blob(  # pyright: ignore[reportUnknownMemberType]
                object_name, retry=DEFAULT_RETRY, timeout=self._timeout
            )
        except _TRANSIENT as exc:
            raise TransientObjectStoreError("GCS stat temporarily failed") from exc
        except _PERMANENT as exc:
            raise PermanentObjectStoreError("GCS stat was rejected") from exc
        return None if blob is None else self._ack(blob, created=False)

    def put_wal_ciphertext_if_absent(
        self,
        path: Path,
        object_name: str,
        metadata: Mapping[str, str],
    ) -> RemoteObjectAck:
        """Publish one bounded WAL staging file with generation-match zero.

        ``upload_from_filename`` is deliberate: past ~8 MiB the SDK switches
        to a resumable session that calls ``tell()``/``seek()`` on its source,
        which only a real file supports (QA #920 block 1 — a streamed
        encrypted reader made every real WAL segment fail with
        UnsupportedOperation; the WAL uploader stages a bounded file first).
        This adapter method is deliberately not a base-backup API.
        """
        blob = self._bucket.blob(object_name)  # pyright: ignore[reportUnknownMemberType]
        blob.metadata = dict(metadata)  # pyright: ignore[reportUnknownMemberType]
        try:
            blob.upload_from_filename(  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
                str(path),
                if_generation_match=0,
                checksum="crc32c",
                retry=DEFAULT_RETRY_IF_GENERATION_SPECIFIED,
                timeout=self._timeout,
            )
            blob.reload(retry=DEFAULT_RETRY, timeout=self._timeout)  # pyright: ignore[reportUnknownMemberType]
            return self._ack(blob, created=True)
        except PreconditionFailed:
            existing = self.stat(object_name)
            if existing is None:
                raise TransientObjectStoreError(
                    "GCS precondition raced with a missing object"
                ) from None
            return existing
        except _TRANSIENT as exc:
            raise TransientObjectStoreError("GCS upload temporarily failed") from exc
        except _PERMANENT as exc:
            raise PermanentObjectStoreError("GCS upload was rejected") from exc
