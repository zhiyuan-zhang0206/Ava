"""Immutable protected-manifest publisher for the COS backend."""

from __future__ import annotations

import hashlib

from services.pitr.checksums import MD5, ObjectChecksum
from services.pitr.cos_client import (
    CosClient,
    CosClientError,
    CosCredentials,
    CosPreconditionFailedError,
    CosTransientError,
)
from services.pitr.object_store import (
    PermanentObjectStoreError,
    RemoteObjectAck,
    TransientObjectStoreError,
)


class CosProtectedManifestPublisher:
    """Publish one small JSON manifest through the same single PUT the
    objects use, then read the stored body back byte-for-byte."""

    def __init__(self, *, credentials: CosCredentials, timeout_seconds: float = 300.0) -> None:
        self._client = CosClient(credentials, timeout_seconds=timeout_seconds)

    @classmethod
    def from_client(cls, client: CosClient) -> CosProtectedManifestPublisher:
        instance = cls.__new__(cls)
        instance._client = client
        return instance

    def put_manifest_if_absent(
        self, *, payload: bytes, object_name: str, metadata: dict[str, str]
    ) -> RemoteObjectAck:
        digest = hashlib.md5(payload).hexdigest()  # noqa: S324 — manifest ACK digest
        created = True
        try:
            self._client.put_object_bytes(object_name, body=payload, metadata=metadata)
        except CosPreconditionFailedError:
            created = False
        except CosTransientError as exc:
            raise TransientObjectStoreError(
                "protected manifest publication temporarily failed"
            ) from exc
        except CosClientError as exc:
            raise PermanentObjectStoreError(
                f"protected manifest publication was rejected: {exc}"
            ) from exc
        row = self._client.head_object(object_name)
        if row is None:
            raise TransientObjectStoreError(
                "protected manifest precondition raced with a missing object"
            )
        if row.etag != digest or row.size != len(payload) or dict(row.metadata) != metadata:
            raise PermanentObjectStoreError("immutable protected manifest differs from its payload")
        body = self._client.get_object_bytes(object_name)
        if body is None or body != payload:
            raise PermanentObjectStoreError("immutable protected manifest differs")
        return RemoteObjectAck(
            object_name=object_name,
            pin_token=row.etag,
            size=len(payload),
            checksum=ObjectChecksum(MD5, digest),
            metadata=dict(row.metadata),
            created=created,
        )
