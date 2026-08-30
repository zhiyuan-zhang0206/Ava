# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

"""Viewer-only, pin-token download for Aliyun OSS restore drills."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import oss2

from services.pitr.checksums import MD5
from services.pitr.object_store import PermanentObjectStoreError
from services.pitr.oss_store import (
    OSSObjectStore,
    _is_not_found,
    _map_error,
    _normalize_etag,
    _ReadResult,
    _user_metadata,
)
from services.pitr.restore_manifest import RestoreObject


class OSSGenerationPinnedObjectReader:
    """Read one pinned immutable object; no write/delete verb on this role.

    ``pin_token`` is the object ETag — OSS GET honors ``If-Match``, so the
    download fails with 412 the moment a different object sits at the name.
    The content MD5 pins the bytes: the stream is hashed while it lands and
    verified before the destination is linked into place.
    """

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
    def from_store(cls, store: OSSObjectStore) -> OSSGenerationPinnedObjectReader:
        instance = cls.__new__(cls)
        instance._store = store
        return instance

    def download_exact(self, expected: RestoreObject, destination: Path) -> None:
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("restore download destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = destination.parent / f".{destination.name}.partial"
        if partial.exists() or partial.is_symlink():
            raise FileExistsError("restore download partial already exists")
        if expected.checksum_algo != MD5:
            raise PermanentObjectStoreError("pinned restore object checksum is not Aliyun OSS MD5")
        etag = _normalize_etag(expected.pin_token)
        if not etag:
            raise PermanentObjectStoreError(
                "pinned restore object pin token is not an Aliyun OSS ETag"
            )
        owned_partial = False
        body = None
        try:
            body = self._open_pinned(expected, etag)
            checksum = hashlib.md5()  # noqa: S324
            size = 0
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            owned_partial = True
            with os.fdopen(fd, "wb") as output:
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    checksum.update(chunk)
                    output.write(chunk)
                    size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size != expected.size or checksum.hexdigest() != expected.checksum_value:
                raise PermanentObjectStoreError("pinned restore object content differs")
            os.link(partial, destination, follow_symlinks=False)
            _fsync_dir(destination.parent)
            partial.unlink()
            _fsync_dir(destination.parent)
            owned_partial = False
        finally:
            if body is not None:
                body.close()
            if owned_partial:
                partial.unlink(missing_ok=True)

    def _open_pinned(self, expected: RestoreObject, etag: str) -> _ReadResult:
        try:
            body = self._store.get_object(expected.object_name, headers={"If-Match": f'"{etag}"'})
        except oss2.exceptions.OssError as exc:
            if _is_not_found(exc) or exc.code == "PreconditionFailed":
                raise PermanentObjectStoreError(
                    "pinned restore object does not exist or differs"
                ) from exc
            raise _map_error("pinned restore download", exc) from exc
        if (
            _normalize_etag(body.etag) != etag
            or body.content_length is None
            or int(body.content_length) != expected.size
            or _user_metadata(body.headers) != dict(expected.metadata)
        ):
            body.close()
            raise PermanentObjectStoreError("pinned restore object properties differ")
        return body


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
