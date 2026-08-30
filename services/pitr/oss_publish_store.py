"""Immutable protected-manifest publisher for the Aliyun OSS backend."""

from __future__ import annotations

from pathlib import Path

import oss2

from services.pitr.checksums import MD5, ObjectChecksum
from services.pitr.object_store import PermanentObjectStoreError, RemoteObjectAck
from services.pitr.oss_store import (
    _FORBID_HEADER,
    OSSObjectStore,
    _b64_md5,
    _is_file_exists,
    _map_error,
    _md5_hex,
    _metadata_headers,
    _normalize_etag,
    _user_metadata,
)


class OSSProtectedManifestPublisher:
    """Publish one small JSON manifest with the server-enforced
    forbid-overwrite precondition, then read the stored body back and
    compare — the same evidence the GCS publisher demands."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        credentials_file: str | Path,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._store = OSSObjectStore(
            endpoint=endpoint,
            bucket=bucket,
            credentials_file=credentials_file,
            timeout_seconds=timeout_seconds,
        )

    @classmethod
    def from_store(cls, store: OSSObjectStore) -> OSSProtectedManifestPublisher:
        instance = cls.__new__(cls)
        instance._store = store
        return instance

    def put_manifest_if_absent(
        self, *, payload: bytes, object_name: str, metadata: dict[str, str]
    ) -> RemoteObjectAck:
        digest = _md5_hex(payload)
        try:
            self._store.put_object(
                object_name,
                payload,
                headers={
                    **_FORBID_HEADER,
                    "Content-MD5": _b64_md5(digest),
                    **_metadata_headers(metadata),
                },
            )
            created = True
        except oss2.exceptions.ServerError as exc:
            if _is_file_exists(exc):
                created = False
            else:
                raise _map_error("OSS protected manifest publish", exc) from exc
        body = self._store.get_object(object_name)
        try:
            stored = body.read()
        finally:
            body.close()
        etag = _normalize_etag(body.etag)
        if (
            body.content_length is None
            or int(body.content_length) != len(payload)
            or stored != payload
            or _user_metadata(body.headers) != metadata
        ):
            raise PermanentObjectStoreError("immutable protected manifest differs")
        return RemoteObjectAck(
            object_name=object_name,
            pin_token=etag,
            size=len(payload),
            checksum=ObjectChecksum(MD5, digest),
            metadata=dict(metadata),
            created=created,
        )
