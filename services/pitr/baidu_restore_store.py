"""Viewer-only, pin-token download for Baidu Netdisk restore drills."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import cast

import httpx

from services.pitr.baidu_pcs import RemoteFile
from services.pitr.baidu_store import BaiduObjectStore
from services.pitr.checksums import MD5, ObjectChecksum
from services.pitr.object_store import PermanentObjectStoreError, TransientObjectStoreError
from services.pitr.restore_manifest import RestoreObject
from services.pitr.token_manager import StoreTokenManager

_DOWNLOAD_UA = "pan.baidu.com"
"""The only User-Agent the download data plane accepts."""


class BaiduGenerationPinnedObjectReader:
    """Read one pinned immutable object; no write/delete verb on this role.

    ``pin_token`` is ``<fs_id>:<content-md5>`` — the fs_id pins the file
    row, the md5 pins the content; both must match the restore object
    before a single byte is written.
    """

    def __init__(
        self,
        *,
        app_root: str,
        token_manager: StoreTokenManager,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._store = BaiduObjectStore(
            app_root=app_root, token_manager=token_manager, timeout_seconds=timeout_seconds
        )
        self._token_manager = token_manager
        self._timeout = timeout_seconds

    def download_exact(self, expected: RestoreObject, destination: Path) -> None:
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("restore download destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = destination.parent / f".{destination.name}.partial"
        if partial.exists() or partial.is_symlink():
            raise FileExistsError("restore download partial already exists")
        if expected.checksum_algo != MD5:
            raise PermanentObjectStoreError(
                "pinned restore object checksum is not Baidu Netdisk MD5"
            )
        fs_id, pin_md5 = self._parse_pin(expected.pin_token)
        owned_partial = False
        try:
            client = self._store._client()
            meta = client.filemetas(fs_id, dlink=True)
            if meta is None or meta.dlink is None:
                raise PermanentObjectStoreError("pinned restore object does not exist")
            self._verify_properties(meta, expected, pin_md5)
            self._verify_sidecar(expected)
            checksum = hashlib.md5()  # noqa: S324
            size = 0
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            owned_partial = True
            stream = None
            try:
                with os.fdopen(fd, "wb") as output:
                    stream = self._download_stream(meta.dlink)
                    for chunk in stream.iter_bytes(1024 * 1024):
                        checksum.update(chunk)
                        output.write(chunk)
                        size += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            except httpx.HTTPError as exc:
                raise TransientObjectStoreError("pinned restore download transport failed") from exc
            finally:
                if stream is not None:
                    stream.close()
            if size != expected.size or checksum.hexdigest() != expected.checksum_value:
                raise PermanentObjectStoreError("pinned restore object content differs")
            os.link(partial, destination, follow_symlinks=False)
            _fsync_dir(destination.parent)
            partial.unlink()
            _fsync_dir(destination.parent)
            owned_partial = False
        finally:
            if owned_partial:
                partial.unlink(missing_ok=True)

    @staticmethod
    def _parse_pin(pin_token: str) -> tuple[int, str]:
        try:
            fs_id_text, pin_md5 = pin_token.split(":", 1)
            return int(fs_id_text), pin_md5
        except ValueError as exc:
            raise PermanentObjectStoreError(
                "pinned restore object pin token is not a Baidu fs_id:md5 pair"
            ) from exc

    @staticmethod
    def _verify_properties(meta: RemoteFile, expected: RestoreObject, pin_md5: str) -> None:
        if (
            meta.size != expected.size
            or str(meta.md5) != pin_md5
            or str(meta.md5) != expected.checksum_value
        ):
            raise PermanentObjectStoreError("pinned restore object properties differ")

    def _verify_sidecar(self, expected: RestoreObject) -> None:
        sidecar = self._store.read_sidecar(expected.object_name)
        if sidecar is None:
            raise PermanentObjectStoreError("pinned restore object lacks its sidecar")
        if (
            str(sidecar["pin_token"]) != expected.pin_token
            or int(sidecar["size"]) != expected.size
            or ObjectChecksum(str(sidecar["checksum_algo"]), str(sidecar["checksum_value"]))
            != ObjectChecksum(expected.checksum_algo, expected.checksum_value)
            or dict(cast(dict[str, str], sidecar["metadata"])) != dict(expected.metadata)
        ):
            raise PermanentObjectStoreError("pinned restore object sidecar differs")

    def _download_stream(self, dlink: str) -> httpx.Response:
        """Open the dlink; ``iter_bytes`` streams without buffering the body."""
        url = f"{dlink}&access_token={self._token_manager.get_access_token()}"
        response = httpx.get(
            url,
            headers={"User-Agent": _DOWNLOAD_UA},
            timeout=self._timeout,
            follow_redirects=True,
        )
        if response.status_code != 200:
            response.close()
            raise TransientObjectStoreError(f"Baidu download HTTP {response.status_code}")
        return response


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
