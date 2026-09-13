"""Identity-bound deletion for policy-owned retention deletions.

The retention executor may delete only the objects a retention plan marks
eligible, and only while the live object still carries the immutable
identity (``pin_token``) the plan recorded. This module owns that narrow
protocol, its outcome vocabulary, and the GCS adapter; other backends
expose their own adapters in their transport modules.

The publish-side contracts (``ObjectStore``,
``RestartableStreamingObjectStore``, the manifest publishers) deliberately
carry no delete verb, so the publishing path stays append-only by
construction and deletion lives only in this role.

Per-backend guarantees:

- GCS: atomic generation-pinned delete (``if_generation_match``): a
  replaced object raises PreconditionFailed and is never deleted.
- OSS: emulated — OSS documents no precondition on DeleteObject and
  answers 204 whether or not the object existed, so its adapter
  re-observes the live ETag, refuses on a mismatch, then deletes, and the
  executor re-observes absence (see ``OSSRetentionDeleteStore``).
"""

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

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

from services.pitr.object_store import (
    PermanentObjectStoreError,
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


class DeleteOutcome(StrEnum):
    """The result of one identity-bound conditional delete attempt."""

    DELETED = "deleted"
    ABSENT = "absent"
    MISMATCH = "mismatch"


class RetentionDeleteStore(Protocol):
    def delete_if_match(self, object_name: str, identity: str) -> DeleteOutcome:
        """Delete ``object_name`` only while the live object carries ``identity``.

        The identity is the object's immutable pinned credential (GCS
        generation, OSS ETag) exactly as the retention plan recorded it.
        ``DELETED`` means the conditional delete applied — the executor
        still re-observes absence before recording success; ``ABSENT``
        means the object was already gone (idempotent, the goal state
        holds); ``MISMATCH`` means a different live identity — nothing was
        deleted. There is deliberately no prefix or bulk delete verb.
        """
        ...


class _DeleteBlobClient(Protocol):
    def delete(self, **kwargs: object) -> None: ...


class _DeleteBucketClient(Protocol):
    def blob(self, name: str) -> _DeleteBlobClient: ...


class GCSRetentionDeleteStore:
    """Generation-pinned conditional deletion; credentials never in argv."""

    def __init__(
        self, *, project: str, bucket: str, credentials_file: Path, timeout_seconds: int = 30
    ) -> None:
        credentials = service_account.Credentials.from_service_account_file(  # pyright: ignore[reportUnknownMemberType]
            str(credentials_file)
        )
        self._bucket = cast(
            _DeleteBucketClient,
            storage.Client(  # pyright: ignore[reportUnknownMemberType]
                project=project, credentials=credentials
            ).bucket(bucket),
        )
        self._timeout = timeout_seconds

    @classmethod
    def from_bucket_client(
        cls, bucket: _DeleteBucketClient, *, timeout_seconds: int = 30
    ) -> GCSRetentionDeleteStore:
        """Construct around a transport-controlled SDK bucket for contract tests."""

        instance = cls.__new__(cls)
        instance._bucket = bucket
        instance._timeout = timeout_seconds
        return instance

    def delete_if_match(self, object_name: str, identity: str) -> DeleteOutcome:
        """Delete iff the live generation still equals ``identity``.

        NotFound means the object is already gone (idempotent success);
        PreconditionFailed means the live generation differs — nothing was
        deleted.
        """

        try:
            generation = int(identity)
        except ValueError as exc:
            raise PermanentObjectStoreError("GCS retention identity is not a generation") from exc
        blob = self._bucket.blob(object_name)
        try:
            blob.delete(
                if_generation_match=generation,
                retry=DEFAULT_RETRY_IF_GENERATION_SPECIFIED,
                timeout=self._timeout,
            )
        except NotFound:
            return DeleteOutcome.ABSENT
        except PreconditionFailed:
            return DeleteOutcome.MISMATCH
        except _TRANSIENT as exc:
            raise TransientObjectStoreError("GCS conditional delete temporarily failed") from exc
        except _PERMANENT as exc:
            raise PermanentObjectStoreError("GCS conditional delete was rejected") from exc
        return DeleteOutcome.DELETED
