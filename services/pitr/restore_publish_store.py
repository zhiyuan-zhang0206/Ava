"""Immutable publisher for the final protected restore-proof manifest."""

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage
from google.cloud.storage.retry import DEFAULT_RETRY_IF_GENERATION_SPECIFIED
from google.oauth2 import service_account

from services.pitr.object_store import PermanentObjectStoreError, RemoteObjectAck


class _ManifestBlob(Protocol):
    name: str
    generation: int | str | None
    size: int | str | None
    crc32c: str | None
    metadata: Mapping[str, str] | None

    def upload_from_string(self, value: bytes, **kwargs: object) -> None: ...

    def download_as_bytes(self, **kwargs: object) -> bytes: ...

    def reload(self, **kwargs: object) -> None: ...


class _ManifestBucket(Protocol):
    def blob(self, name: str, **kwargs: object) -> _ManifestBlob: ...

    def get_blob(self, name: str, **kwargs: object) -> _ManifestBlob | None: ...


class GCSProtectedManifestPublisher:
    def __init__(
        self, *, project: str, bucket: str, credentials_file: Path, timeout_seconds: int = 60
    ) -> None:
        credentials = service_account.Credentials.from_service_account_file(str(credentials_file))
        self._bucket = cast(
            _ManifestBucket,
            storage.Client(project=project, credentials=credentials).bucket(bucket),
        )
        self._timeout = timeout_seconds

    @classmethod
    def from_bucket_client(
        cls, bucket: _ManifestBucket, *, timeout_seconds: int = 60
    ) -> GCSProtectedManifestPublisher:
        instance = cls.__new__(cls)
        instance._bucket = bucket
        instance._timeout = timeout_seconds
        return instance

    def put_manifest_if_absent(
        self, *, payload: bytes, object_name: str, metadata: dict[str, str]
    ) -> RemoteObjectAck:
        blob = self._bucket.blob(object_name)
        blob.metadata = dict(metadata)
        created = True
        try:
            blob.upload_from_string(
                payload,
                content_type="application/json",
                if_generation_match=0,
                checksum="crc32c",
                retry=DEFAULT_RETRY_IF_GENERATION_SPECIFIED,
                timeout=self._timeout,
            )
            blob.reload(timeout=self._timeout)
        except PreconditionFailed:
            created = False
            existing = self._bucket.get_blob(object_name, timeout=self._timeout)
            if existing is None:
                raise PermanentObjectStoreError(
                    "protected manifest precondition raced with a missing object"
                ) from None
            blob = existing
        if blob.generation is None or blob.size is None or blob.crc32c is None:
            raise PermanentObjectStoreError("protected manifest omitted object properties")
        body = blob.download_as_bytes(
            if_generation_match=int(blob.generation), timeout=self._timeout
        )
        if body != payload or dict(blob.metadata or {}) != metadata:
            raise PermanentObjectStoreError("immutable protected manifest differs")
        return RemoteObjectAck(
            blob.name,
            int(blob.generation),
            int(blob.size),
            blob.crc32c,
            dict(blob.metadata or {}),
            created,
        )
