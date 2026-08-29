"""Google Cloud Storage adapter for the owned immutable-object boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import BinaryIO

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


class GCSObjectStore:
    """One bucket-scoped adapter; credentials never enter process argv."""

    def __init__(
        self, *, project: str, bucket: str, credentials_file: Path, timeout_seconds: float = 30
    ) -> None:
        credentials = service_account.Credentials.from_service_account_file(str(credentials_file))
        self._bucket = storage.Client(project=project, credentials=credentials).bucket(bucket)
        self._timeout = timeout_seconds

    @staticmethod
    def _ack(blob: storage.Blob, *, created: bool) -> RemoteObjectAck:
        if blob.generation is None or blob.size is None or blob.crc32c is None:
            raise TransientObjectStoreError("GCS object omitted verification properties")
        return RemoteObjectAck(
            object_name=blob.name,
            generation=int(blob.generation),
            size=int(blob.size),
            crc32c=blob.crc32c,
            metadata=dict(blob.metadata or {}),
            created=created,
        )

    def stat(self, object_name: str) -> RemoteObjectAck | None:
        try:
            blob = self._bucket.get_blob(object_name, retry=DEFAULT_RETRY, timeout=self._timeout)
        except _TRANSIENT as exc:
            raise TransientObjectStoreError("GCS stat temporarily failed") from exc
        except _PERMANENT as exc:
            raise PermanentObjectStoreError("GCS stat was rejected") from exc
        return None if blob is None else self._ack(blob, created=False)

    def put_stream_if_absent(
        self,
        open_source: Callable[[], BinaryIO],
        size: int,
        object_name: str,
        metadata: Mapping[str, str],
    ) -> RemoteObjectAck:
        blob = self._bucket.blob(object_name)
        blob.metadata = dict(metadata)
        try:
            with open_source() as source:
                blob.upload_from_file(
                    source,
                    size=size,
                    rewind=False,
                    if_generation_match=0,
                    checksum="crc32c",
                    retry=DEFAULT_RETRY_IF_GENERATION_SPECIFIED,
                    timeout=self._timeout,
                )
            blob.reload(retry=DEFAULT_RETRY, timeout=self._timeout)
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
