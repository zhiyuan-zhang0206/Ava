"""Viewer-only, ETag-pinned object reads for COS restore drills."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import httpx

from services.pitr.checksums import MD5
from services.pitr.cos_client import (
    CosClient,
    CosClientError,
    CosCredentials,
    CosNotFoundError,
    CosPreconditionFailedError,
    CosTransientError,
    response_metadata,
)
from services.pitr.object_store import PermanentObjectStoreError, TransientObjectStoreError
from services.pitr.restore_manifest import RestoreObject


class CosGenerationPinnedObjectReader:
    """Read one pinned immutable object; no write/delete verb on this role.

    ``pin_token`` is the ETag — for a simple PUT it IS the object content
    MD5, so the pinned read pins both the server identity (If-Match on the
    GET) and the content digest (recomputed while streaming).
    """

    def __init__(self, *, credentials: CosCredentials, timeout_seconds: float = 300.0) -> None:
        self._client = CosClient(credentials, timeout_seconds=timeout_seconds)

    @classmethod
    def from_client(cls, client: CosClient) -> CosGenerationPinnedObjectReader:
        instance = cls.__new__(cls)
        instance._client = client
        return instance

    def download_exact(self, expected: RestoreObject, destination: Path) -> None:
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("restore download destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = destination.parent / f".{destination.name}.partial"
        if partial.exists() or partial.is_symlink():
            raise FileExistsError("restore download partial already exists")
        if expected.checksum_algo != MD5:
            raise PermanentObjectStoreError("pinned restore object checksum is not COS MD5")
        pin = self._pin_etag(expected)
        owned_partial = False
        try:
            response = self._client.get_object(expected.object_name, if_match=pin)
            self._verify_response_headers(response, expected, pin)
            checksum = hashlib.md5()  # noqa: S324
            size = 0
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            owned_partial = True
            try:
                with os.fdopen(fd, "wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        checksum.update(chunk)
                        output.write(chunk)
                        size += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            except httpx.HTTPError as exc:
                raise TransientObjectStoreError("pinned restore download transport failed") from exc
            finally:
                response.close()
            if size != expected.size or checksum.hexdigest() != expected.checksum_value:
                raise PermanentObjectStoreError("pinned restore object content differs")
            os.link(partial, destination, follow_symlinks=False)
            _fsync_dir(destination.parent)
            partial.unlink()
            _fsync_dir(destination.parent)
            owned_partial = False
        except CosNotFoundError as exc:
            raise PermanentObjectStoreError("pinned restore object does not exist") from exc
        except CosPreconditionFailedError as exc:
            raise PermanentObjectStoreError("pinned restore object changed") from exc
        except CosTransientError as exc:
            raise TransientObjectStoreError("pinned restore download temporarily failed") from exc
        except CosClientError as exc:
            raise PermanentObjectStoreError("pinned restore download was rejected") from exc
        finally:
            if owned_partial:
                partial.unlink(missing_ok=True)

    @staticmethod
    def _pin_etag(expected: RestoreObject) -> str:
        if "-" in expected.pin_token:
            raise PermanentObjectStoreError(
                "pinned restore object pin token is a COS multipart ETag"
            )
        return expected.pin_token

    @staticmethod
    def _verify_response_headers(
        response: httpx.Response, expected: RestoreObject, pin: str
    ) -> None:
        etag = (response.headers.get("etag") or "").strip().strip('"')
        content_length = response.headers.get("content-length")
        metadata = response_metadata(response.headers)
        if (
            etag != pin
            or content_length is None
            or int(content_length) != expected.size
            or metadata != dict(expected.metadata)
        ):
            raise PermanentObjectStoreError("pinned restore object properties differ")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
