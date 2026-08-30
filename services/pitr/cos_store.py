"""Tencent Cloud COS adapter for the owned immutable-object boundary.

One class serves the ObjectStore and RestartableStreamingObjectStore
roles internally (the role contracts stay separate at the callers).
Every object is published with a single PUT (COS simple-PUT ceiling
5 GiB — enforced, never silently switched to multipart):

- iff-absent is the S3 conditional write ``If-None-Match: *``; a 412
  falls back to head + identity verification of the existing object
  (the global-immutability race is resolved by name: base names embed
  the candidate hash and WAL names pin a single server-side segment,
  so the same canonical name always carries the same content).
- content integrity is backend-verified twice: the request carries
  ``Content-MD5`` (COS rejects a mismatch) and the response ETag of a
  simple PUT is MD5 — the adapter asserts ``etag == md5`` after a
  read-back HEAD, so the ACK checksum cannot drift from the stored
  object.
- pin_token = ETag; a composite multipart ETag (contains ``-``) on
  head/stat is never adopted — foreign objects fail closed instead of
  silently onboarding a non-MD5 identity.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

from services.pitr.base_object_store import RestartableEncryptedSource
from services.pitr.checksums import MD5, ObjectChecksum
from services.pitr.cos_client import (
    CosClient,
    CosClientError,
    CosCredentials,
    CosNotFoundError,
    CosPreconditionFailedError,
    CosTransientError,
)
from services.pitr.object_store import (
    ObjectStoreError,
    PermanentObjectStoreError,
    RemoteObjectAck,
    TransientObjectStoreError,
)

_HASH_CHUNK_BYTES = 8 * 1024 * 1024
"""Read/write granularity; the base packer and WAL staging use 8 MiB."""


class CosObjectStore:
    """One bucket-scoped adapter; credentials never enter process argv.

    ``SIMPLE_PUT_LIMIT_BYTES`` is a class attribute (not a module global) so
    contract tests can shrink it on the class the adapter instance binds to
    — the windows import-surface suite re-imports these modules, which
    would leave a module-global patch on a module the tested class no
    longer reads.
    """

    SIMPLE_PUT_LIMIT_BYTES = 5 * 1024**3
    """COS caps a simple PUT at 5 GiB; multipart (composite ETag != content
    MD5) is deliberately not used here, so anything larger fails permanent
    with the packaging instruction instead of silently weakening the ACK
    identity."""

    def __init__(self, *, credentials: CosCredentials, timeout_seconds: float = 300.0) -> None:
        self._client = CosClient(credentials, timeout_seconds=timeout_seconds)

    @classmethod
    def from_client(cls, client: CosClient) -> CosObjectStore:
        """Construct around a transport-controlled client for contract tests."""
        instance = cls.__new__(cls)
        instance._client = client
        return instance

    # ── ObjectStore role ──

    def stat(self, object_name: str) -> RemoteObjectAck | None:
        """Re-observe an object: the HEAD identity is the whole ACK (ETag
        == MD5 content digest for every object this backend publishes)."""
        try:
            row = self._client.head_object(object_name)
        except CosClientError as exc:
            raise self._map_error("COS stat", exc) from exc
        if row is None:
            return None
        if "-" in row.etag:
            raise PermanentObjectStoreError(
                "immutable COS object carries a multipart ETag and is not adoptable"
            )
        return RemoteObjectAck(
            object_name=object_name,
            pin_token=row.etag,
            size=row.size,
            checksum=ObjectChecksum(MD5, row.etag),
            metadata=dict(row.metadata),
            created=False,
        )

    def put_wal_ciphertext_if_absent(
        self,
        path: Path,
        object_name: str,
        metadata: Mapping[str, str],
    ) -> RemoteObjectAck:
        """Publish one bounded WAL staging file (seekable, per the shared
        ObjectStore contract) through a single Content-MD5-verified PUT."""
        size = path.stat().st_size
        self._assert_size_ceiling(size)
        md5, sha256 = _file_digests(path)
        created = self._publish_once(
            object_name=object_name,
            size=size,
            md5=md5,
            sha256=sha256,
            body=_read_file_chunks(path, size),
            metadata=dict(metadata),
        )
        return self._confirm_published(
            object_name=object_name,
            size=size,
            md5=md5,
            metadata=dict(metadata),
            created=created,
        )

    # ── RestartableStreamingObjectStore role ──

    def put_base_if_absent(
        self,
        *,
        source: RestartableEncryptedSource,
        object_name: str,
        metadata: Mapping[str, str],
        cancelled: Callable[[], bool] = lambda: False,
    ) -> RemoteObjectAck:
        """Stream a deterministic restartable ciphertext through one PUT.

        Pass 1 walks ``iter_chunks`` once (MD5 + SHA256 of the whole
        stream); pass 2 re-walks the same deterministic bytes as the
        request body — the restartable contract makes the two walks
        identical, and COS verifies the advertised Content-MD5 against
        what actually arrives.
        """
        size = source.ciphertext_size
        self._assert_size_ceiling(size)
        if cancelled():
            raise RuntimeError("COS base upload cancelled before hashing")
        md5, sha256, walked = _digest_source(source, cancelled)
        if walked != size:
            raise PermanentObjectStoreError("immutable COS base stream size differs from its plan")
        if cancelled():
            raise RuntimeError("COS base upload cancelled after hashing")

        def body() -> Iterator[bytes]:
            for chunk in source.iter_chunks():
                if cancelled():
                    raise RuntimeError("COS base upload cancelled at chunk boundary")
                yield chunk
                if cancelled():
                    raise RuntimeError("COS base upload cancelled at chunk boundary")

        created = self._publish_once(
            object_name=object_name,
            size=size,
            md5=md5,
            sha256=sha256,
            body=body(),
            metadata=dict(metadata),
            cancelled=cancelled,
        )
        ack = self._confirm_published(
            object_name=object_name,
            size=size,
            md5=md5,
            metadata=dict(metadata),
            created=created,
        )
        if cancelled():
            raise RuntimeError("COS base upload lost ownership before local publication")
        if (
            ack.object_name != object_name
            or not ack.pin_token
            or ack.size != size
            or ack.checksum != ObjectChecksum(MD5, md5)
            or dict(ack.metadata) != dict(metadata)
        ):
            raise PermanentObjectStoreError("immutable COS base object differs")
        return ack

    # ── shared publish engine ──

    def _publish_once(
        self,
        *,
        object_name: str,
        size: int,
        md5: str,
        sha256: str,
        body: Iterator[bytes],
        metadata: Mapping[str, str],
        cancelled: Callable[[], bool] = lambda: False,
    ) -> bool:
        """PUT with If-None-Match; True when this call created the object."""
        try:
            self._client.put_object_stream(
                object_name,
                body=body,
                content_length=size,
                content_md5=md5,
                payload_sha256=sha256,
                metadata=metadata,
            )
            return True
        except CosPreconditionFailedError:
            return False
        except CosTransientError as exc:
            raise TransientObjectStoreError("COS upload temporarily failed") from exc
        except CosClientError as exc:
            raise PermanentObjectStoreError("COS upload was rejected") from exc
        except RuntimeError:
            # The restartable body generator raises the ownership check
            # itself; an httpx transport must not re-wrap it.
            if cancelled():
                raise
            raise TransientObjectStoreError("COS upload stream aborted") from None

    def _confirm_published(
        self,
        *,
        object_name: str,
        size: int,
        md5: str,
        metadata: Mapping[str, str],
        created: bool,
    ) -> RemoteObjectAck:
        """Read back the HEAD identity and require exact equality with the
        local archive before any ACK leaves the adapter."""
        row = self._client.head_object(object_name)
        if row is None:
            raise TransientObjectStoreError("COS precondition raced with a missing object")
        if "-" in row.etag:
            raise PermanentObjectStoreError(
                "immutable COS object carries a multipart ETag and is not adoptable"
            )
        if row.etag != md5 or row.size != size or dict(row.metadata) != metadata:
            raise PermanentObjectStoreError("immutable COS object differs from the local archive")
        return RemoteObjectAck(
            object_name=object_name,
            pin_token=row.etag,
            size=row.size,
            checksum=ObjectChecksum(MD5, row.etag),
            metadata=dict(row.metadata),
            created=created,
        )

    def _assert_size_ceiling(self, size: int) -> None:
        if size > self.SIMPLE_PUT_LIMIT_BYTES:
            raise PermanentObjectStoreError(
                f"COS object exceeds the simple-PUT limit "
                f"({size} > {self.SIMPLE_PUT_LIMIT_BYTES}); the archive needs "
                f"multi-part packaging before this backend can carry it"
            )

    @staticmethod
    def _map_error(operation: str, exc: CosClientError) -> ObjectStoreError:
        if isinstance(exc, CosTransientError):
            return TransientObjectStoreError(f"{operation} temporarily failed: {exc}")
        if isinstance(exc, CosNotFoundError):
            return TransientObjectStoreError(f"{operation} raced with a missing object")
        return PermanentObjectStoreError(f"{operation} was rejected: {exc}")


def _file_digests(path: Path) -> tuple[str, str]:
    md5 = hashlib.md5()  # noqa: S324
    sha256 = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(_HASH_CHUNK_BYTES), b""):
            md5.update(chunk)
            sha256.update(chunk)
    return md5.hexdigest(), sha256.hexdigest()


def _read_file_chunks(path: Path, size: int) -> Iterator[bytes]:
    with path.open("rb") as source:
        remaining = size
        while remaining > 0:
            chunk = source.read(min(_HASH_CHUNK_BYTES, remaining))
            if not chunk:
                raise PermanentObjectStoreError("COS upload source shrank while reading")
            remaining -= len(chunk)
            yield chunk


def _digest_source(
    source: RestartableEncryptedSource, cancelled: Callable[[], bool]
) -> tuple[str, str, int]:
    md5 = hashlib.md5()  # noqa: S324
    sha256 = hashlib.sha256()
    walked = 0
    for chunk in source.iter_chunks():
        if cancelled():
            raise RuntimeError("COS base upload cancelled at chunk boundary")
        md5.update(chunk)
        sha256.update(chunk)
        walked += len(chunk)
    return md5.hexdigest(), sha256.hexdigest(), walked
