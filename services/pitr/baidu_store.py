"""Baidu Netdisk adapter for the owned immutable-object boundary.

One class serves the ObjectStore and RestartableStreamingObjectStore
roles internally (the role contracts stay separate at the callers). The
upload engine implements the official three-phase flow with the CTO
constraints for this deployment:

- SVIP shard spec: 32 MiB shards, at most 1024 of them, single file at
  most 20 GB — every upload asserts both and fails permanently past the
  ceiling (the 20 GB bound dominates: 1024 x 32 MiB = 32 GiB).
- iff-absent is approximated with content-addressed naming + precreate
  rtype=3 (same content -> rapid transfer; different content -> errno 2,
  which this adapter maps to a permanent collision) + a post-upload
  read-back of size/md5 through filemetas.
- Metadata has no PCS equivalent, so every object carries a sidecar
  ``<object_name>.ack.json`` beside it, written iff-absent with the same
  content-addressed engine and read back before any ACK is returned.
- Resumption: each attempt re-precreates with the same block list; the
  server reports which shards it still wants, so an outer retry resumes
  without re-uploading shards it already holds (P0 live smoke verifies
  the exact missing-block behavior across sessions).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import httpx

from services.pitr.baidu_pcs import (
    SVIP_SHARD_BYTES,
    SVIP_SINGLE_FILE_LIMIT_BYTES,
    PcsClient,
    PcsError,
    PcsTransientError,
    RemoteFile,
)
from services.pitr.base_object_store import RestartableEncryptedSource
from services.pitr.checksums import MD5, ObjectChecksum
from services.pitr.object_store import (
    ObjectStoreError,
    PermanentObjectStoreError,
    RemoteObjectAck,
    TransientObjectStoreError,
)
from services.pitr.token_manager import StoreTokenManager

MAX_SHARDS = 1024
"""Platform ceiling on shard count (docs); the 20 GB file bound hits first."""

_RTYPE_CONTENT_ADDRESSED = 3
"""Precreate naming policy: same content -> rapid transfer, different
content -> error. The rtype table differs across doc revisions; the P0
smoke pins this value against the live API."""

_DOWNLOAD_UA = "pan.baidu.com"
"""The only User-Agent the download data plane accepts."""


class BaiduObjectStore:
    """One app-root-scoped adapter; the token manager supplies access tokens."""

    def __init__(
        self,
        *,
        app_root: str,
        token_manager: StoreTokenManager,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._app_root = app_root.rstrip("/")
        self._token_manager = token_manager
        self._timeout = timeout_seconds

    @property
    def app_root(self) -> str:
        return self._app_root

    def read_sidecar(self, object_name: str) -> dict[str, Any] | None:
        """Public read-back of an object's sidecar (inventory + stat use)."""
        return self._read_sidecar(object_name)

    def _client(self) -> PcsClient:
        return PcsClient(self._token_manager.get_access_token(), timeout=self._timeout)

    def _path(self, object_name: str) -> str:
        return f"{self._app_root}/{object_name}"

    # ── ObjectStore role ──

    def stat(self, object_name: str) -> RemoteObjectAck | None:
        """Re-observe an object: sidecar for identity, filemetas to confirm
        the file still matches it. None when either piece is missing."""
        sidecar = self._read_sidecar(object_name)
        if sidecar is None:
            return None
        try:
            row = self._file_row(object_name)
        except PcsError as exc:
            raise self._map_error("Baidu stat", exc) from exc
        if row is None:
            return None
        # The PCS row md5 is Baidu's encrypted server digest (live P0
        # smoke: it never equals the content md5), so the row is pinned
        # by fs_id + that digest and the size; the sidecar carries the
        # real content checksum for downstream verification.
        if row.size != int(sidecar["size"]) or f"{row.fs_id}:{row.md5}" != str(
            sidecar["pin_token"]
        ):
            raise PermanentObjectStoreError("immutable Baidu object differs from its sidecar")
        return RemoteObjectAck(
            object_name=object_name,
            pin_token=str(sidecar["pin_token"]),
            size=row.size,
            checksum=ObjectChecksum(str(sidecar["checksum_algo"]), str(sidecar["checksum_value"])),
            metadata=dict(cast(dict[str, str], sidecar["metadata"])),
            created=False,
        )

    def put_wal_ciphertext_if_absent(
        self,
        path: Path,
        object_name: str,
        metadata: Mapping[str, str],
    ) -> RemoteObjectAck:
        """Publish one bounded WAL staging file (seekable, per the shared
        ObjectStore contract) through the three-phase engine."""
        size = path.stat().st_size
        self._assert_size_limits(size)
        block_md5s, whole_md5 = _file_digests(path, size)

        def upload_missing(uploadid: str, indexes: Sequence[int]) -> None:
            for index in indexes:
                self._client().upload_part(
                    path=self._path(object_name),
                    uploadid=uploadid,
                    partseq=index,
                    data=_read_file_block(path, index, size),
                )

        return self._upload_if_absent(
            object_name=object_name,
            size=size,
            block_md5s=block_md5s,
            whole_md5=whole_md5,
            upload_missing=upload_missing,
            metadata=dict(metadata),
            cancelled=lambda: False,
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
        """Stream a deterministic restartable ciphertext through the engine.

        Pass 1 walks ``iter_chunks`` once to build the shard MD5 list and
        the whole-file MD5; pass 2 re-walks the same deterministic bytes
        once to upload whichever shards the precreate response still
        wants — the restartable contract makes the two walks identical.
        """
        size = source.ciphertext_size
        self._assert_size_limits(size)
        if cancelled():
            raise RuntimeError("Baidu base upload cancelled before hashing")
        block_md5s: list[str] = []
        whole = hashlib.md5()  # noqa: S324
        block = hashlib.md5()  # noqa: S324
        block_bytes = 0
        for chunk in source.iter_chunks():
            if cancelled():
                raise RuntimeError("Baidu base upload cancelled at chunk boundary")
            whole.update(chunk)
            offset = 0
            while offset < len(chunk):
                take = min(len(chunk) - offset, SVIP_SHARD_BYTES - block_bytes)
                block.update(chunk[offset : offset + take])
                block_bytes += take
                offset += take
                if block_bytes == SVIP_SHARD_BYTES:
                    block_md5s.append(block.hexdigest())
                    block = hashlib.md5()  # noqa: S324
                    block_bytes = 0
        if block_bytes:
            block_md5s.append(block.hexdigest())
        whole_md5 = whole.hexdigest()

        def upload_missing(uploadid: str, indexes: Sequence[int]) -> None:
            wanted = set(indexes)
            for index, data in _stream_blocks(source, len(block_md5s)):
                if cancelled():
                    raise RuntimeError("Baidu base upload cancelled at shard boundary")
                if index not in wanted:
                    continue
                self._client().upload_part(
                    path=self._path(object_name), uploadid=uploadid, partseq=index, data=data
                )

        return self._upload_if_absent(
            object_name=object_name,
            size=size,
            block_md5s=block_md5s,
            whole_md5=whole_md5,
            upload_missing=upload_missing,
            metadata=dict(metadata),
            cancelled=cancelled,
        )

    # ── shared three-phase engine ──

    def _upload_if_absent(
        self,
        *,
        object_name: str,
        size: int,
        block_md5s: list[str],
        whole_md5: str,
        upload_missing: Callable[[str, Sequence[int]], None],
        metadata: Mapping[str, str],
        cancelled: Callable[[], bool],
        with_sidecar: bool = True,
    ) -> RemoteObjectAck:
        self._assert_size_limits(size)
        path = self._path(object_name)
        try:
            pre = self._client().precreate(
                path=path, size=size, block_list=block_md5s, rtype=_RTYPE_CONTENT_ADDRESSED
            )
        except PcsError as exc:
            raise self._map_error("Baidu precreate", exc) from exc
        if pre.return_type == 2:
            # Rapid transfer: the same content already exists; adopt the
            # existing object after verifying it matches, then reconcile
            # the sidecar (idempotent: same content -> same sidecar).
            if cancelled():
                raise RuntimeError("Baidu upload cancelled before existing-object adoption")
            ack = self._verify_existing(object_name, size, whole_md5, metadata)
            if with_sidecar:
                self._write_sidecar_if_absent(object_name, ack)
            return ack
        if pre.return_type == 3:
            raise PermanentObjectStoreError(
                "immutable Baidu object exists with different content under the canonical name"
            )
        # Live API: precreate reports the missing shards as part indexes
        # ("0".."n-1"), not as md5s — the docs disagree, so both shapes are
        # accepted and out-of-range indexes fail closed (P0 smoke finding).
        indexes = [
            int(digest) if digest.isdigit() else self._block_index(block_md5s, digest)
            for digest in pre.missing_blocks
        ]
        if any(index < 0 or index >= len(block_md5s) for index in indexes):
            raise PermanentObjectStoreError("Baidu precreate asked for an out-of-range shard index")
        try:
            upload_missing(pre.uploadid, indexes)
        except PcsError as exc:
            raise self._map_error("Baidu shard upload", exc) from exc
        if cancelled():
            raise RuntimeError("Baidu upload cancelled before create")
        try:
            row = self._client().create(
                path=path,
                size=size,
                block_list=block_md5s,
                uploadid=pre.uploadid,
            )
        except PcsError as exc:
            raise self._map_error("Baidu create", exc) from exc
        ack = self._ack_from_row(object_name, row, size, whole_md5, metadata, created=True)
        if with_sidecar:
            self._write_sidecar_if_absent(object_name, ack)
        return ack

    @staticmethod
    def _block_index(block_md5s: Sequence[str], digest: str) -> int:
        try:
            return list(block_md5s).index(digest)
        except ValueError as exc:
            raise PermanentObjectStoreError(
                "Baidu precreate asked for an unknown shard digest"
            ) from exc

    def _verify_existing(
        self, object_name: str, size: int, whole_md5: str, metadata: Mapping[str, str]
    ) -> RemoteObjectAck:
        """Adopt an object the platform reports as same-content (rapid
        transfer): the read-back row must match size and md5 exactly, or
        the canonical name holds content we cannot adopt."""
        try:
            row = self._file_row(object_name)
        except PcsError as exc:
            raise self._map_error("Baidu existing-object read-back", exc) from exc
        # return_type=2 is Baidu's own content-address match on the block
        # md5s; the row md5 is an encrypted server digest (never the
        # content md5), so size is the comparable property here.
        if row is None or row.size != size:
            raise PermanentObjectStoreError("immutable Baidu object differs from the local archive")
        return self._ack_from_row(object_name, row, size, whole_md5, metadata, created=False)

    def _file_row(self, object_name: str) -> RemoteFile | None:
        """Resolve one object path to its PCS row via the parent listing."""
        directory, _name = object_name.rsplit("/", 1)
        start = 0
        while True:
            rows = self._client().list_dir(f"{self._app_root}/{directory}", start=start)
            for row in rows:
                if row.path == self._path(object_name):
                    return row
            if len(rows) < 1000:
                return None
            start += len(rows)

    @staticmethod
    def _ack_from_row(
        object_name: str,
        row: RemoteFile,
        size: int,
        whole_md5: str,
        metadata: Mapping[str, str],
        *,
        created: bool,
    ) -> RemoteObjectAck:
        # The row md5 is an encrypted server digest, not the content md5:
        # it identifies the row (pin_token) while the content is carried
        # by the local whole_md5 checksum and verified end to end at
        # restore time from the downloaded bytes.
        if row.size != size:
            raise PermanentObjectStoreError("immutable Baidu object differs from the local archive")
        return RemoteObjectAck(
            object_name=object_name,
            pin_token=f"{row.fs_id}:{row.md5}",
            size=size,
            checksum=ObjectChecksum(MD5, whole_md5),
            metadata=dict(metadata),
            created=created,
        )

    def _assert_size_limits(self, size: int) -> None:
        if size > SVIP_SINGLE_FILE_LIMIT_BYTES:
            raise PermanentObjectStoreError(
                f"Baidu object exceeds the SVIP single-file limit "
                f"({size} > {SVIP_SINGLE_FILE_LIMIT_BYTES}); the base backup needs "
                f"multi-part packaging before this backend can carry it"
            )
        if size // SVIP_SHARD_BYTES + (1 if size % SVIP_SHARD_BYTES else 0) > MAX_SHARDS:
            raise PermanentObjectStoreError(
                f"Baidu object exceeds the {MAX_SHARDS}-shard ceiling "
                f"at {SVIP_SHARD_BYTES}-byte shards"
            )

    # ── sidecar ──

    def _sidecar_upload_missing(
        self, sidecar_name: str, data: bytes
    ) -> Callable[[str, Sequence[int]], None]:
        def upload(uploadid: str, indexes: Sequence[int]) -> None:
            for index in indexes:
                start = index * SVIP_SHARD_BYTES
                self._client().upload_part(
                    path=self._path(sidecar_name),
                    uploadid=uploadid,
                    partseq=index,
                    data=data[start : start + SVIP_SHARD_BYTES],
                )

        return upload

    def _write_sidecar_if_absent(self, object_name: str, ack: RemoteObjectAck) -> None:
        """Publish ``<object_name>.ack.json`` iff absent; an existing sidecar
        must carry the identical identity or the object is not adoptable."""
        payload = {
            "object_name": ack.object_name,
            "pin_token": ack.pin_token,
            "size": ack.size,
            "checksum_algo": ack.checksum.algo,
            "checksum_value": ack.checksum.value,
            "metadata": dict(ack.metadata),
        }
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sidecar_name = f"{object_name}.ack.json"
        existing = self._read_sidecar(object_name)
        if existing is not None:
            if existing == payload:
                return
            # Crash-retry artifact (live P0 smoke): create-on-existing
            # replaces the file object with a NEW fs_id, so a retry
            # re-derives a pin that only the pin_token differs in. Adopt
            # the new pin when the content identity (size, checksum,
            # metadata) is unchanged; any other drift stays permanent.
            same = {key: existing.get(key) for key in existing if key != "pin_token"}
            wanted = {key: payload.get(key) for key in payload if key != "pin_token"}
            if set(existing) != set(payload) or same != wanted:
                raise PermanentObjectStoreError("immutable Baidu object sidecar differs")
        with tempfile.TemporaryDirectory(prefix="baidu-sidecar-") as scratch:
            staged = Path(scratch) / "sidecar.json"
            staged.write_bytes(data)
            block_md5s, whole_md5 = _file_digests(staged, len(data))
            self._upload_if_absent(
                object_name=sidecar_name,
                size=len(data),
                block_md5s=block_md5s,
                whole_md5=whole_md5,
                upload_missing=self._sidecar_upload_missing(sidecar_name, data),
                metadata={
                    "ava-sidecar-of": ack.object_name,
                    "ava-archive-name": ack.metadata.get("ava-archive-name", ""),
                },
                cancelled=lambda: False,
                with_sidecar=False,
            )

    def _read_sidecar(self, object_name: str) -> dict[str, Any] | None:
        """Read and validate the sidecar; None when absent, error when the
        content is not a well-formed ACK mirror."""
        sidecar_name = f"{object_name}.ack.json"
        try:
            row = self._file_row(sidecar_name)
        except PcsError as exc:
            raise self._map_error("Baidu sidecar lookup", exc) from exc
        if row is None:
            return None
        # The sidecar row md5 is the encrypted server digest and cannot
        # verify the downloaded bytes; the JSON schema + the identity
        # cross-checks below are the validation surface.
        data = self._download_bytes(row)
        try:
            payload = json.loads(data.decode())
        except (ValueError, UnicodeDecodeError) as exc:
            raise PermanentObjectStoreError("Baidu object sidecar is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise PermanentObjectStoreError("Baidu object sidecar is not an object")
        sidecar = cast(dict[str, Any], payload)
        if sidecar.get("object_name") != object_name:
            raise PermanentObjectStoreError("Baidu object sidecar names a different object")
        try:
            ObjectChecksum(str(sidecar["checksum_algo"]), str(sidecar["checksum_value"]))
            int(sidecar["size"])
            str(sidecar["pin_token"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PermanentObjectStoreError("Baidu object sidecar identity is malformed") from exc
        return sidecar

    def _download_bytes(self, row: RemoteFile) -> bytes:
        """Download one small object (sidecar-sized) through the dlink."""
        try:
            meta = self._client().filemetas(row.fs_id, dlink=True)
        except PcsError as exc:
            raise self._map_error("Baidu sidecar filemetas", exc) from exc
        if meta is None or meta.dlink is None:
            raise TransientObjectStoreError("Baidu sidecar download link is unavailable")
        try:
            response = httpx.get(
                f"{meta.dlink}&access_token={self._token_manager.get_access_token()}",
                headers={"User-Agent": _DOWNLOAD_UA},
                timeout=self._timeout,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise TransientObjectStoreError("Baidu sidecar download transport failed") from exc
        if response.status_code != 200:
            raise TransientObjectStoreError(f"Baidu sidecar download HTTP {response.status_code}")
        return response.content

    @staticmethod
    def _map_error(operation: str, exc: PcsError) -> ObjectStoreError:
        if isinstance(exc, PcsTransientError):
            return TransientObjectStoreError(f"{operation} temporarily failed: {exc}")
        return PermanentObjectStoreError(f"{operation} was rejected: {exc}")


# ── digest helpers ──


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324 — PCS row digest


def _file_digests(path: Path, size: int) -> tuple[list[str], str]:
    block_md5s: list[str] = []
    whole = hashlib.md5()  # noqa: S324
    with path.open("rb") as source:
        remaining = size
        while remaining > 0:
            data = source.read(min(SVIP_SHARD_BYTES, remaining))
            if not data:
                raise PermanentObjectStoreError("Baidu upload source shrank while hashing")
            whole.update(data)
            block_md5s.append(hashlib.md5(data).hexdigest())  # noqa: S324
            remaining -= len(data)
    return block_md5s, whole.hexdigest()


def _read_file_block(path: Path, index: int, size: int) -> bytes:
    offset = index * SVIP_SHARD_BYTES
    if offset >= size:
        raise PermanentObjectStoreError("Baidu upload asked for a shard past the object end")
    with path.open("rb") as source:
        source.seek(offset)
        data = source.read(min(SVIP_SHARD_BYTES, size - offset))
    if len(data) != min(SVIP_SHARD_BYTES, size - offset):
        raise PermanentObjectStoreError("Baidu upload source shrank while reading a shard")
    return data


def _stream_blocks(
    source: RestartableEncryptedSource, block_count: int
) -> Iterator[tuple[int, bytes]]:
    """Yield every shard of a restartable stream in one walk.

    Deterministic bytes make this second walk identical to the hashing
    walk, so each yielded shard's digest matches the block list position.
    """
    collected = bytearray()
    index = 0
    for chunk in source.iter_chunks():
        collected.extend(chunk)
        while len(collected) >= SVIP_SHARD_BYTES:
            block = bytes(collected[:SVIP_SHARD_BYTES])
            del collected[:SVIP_SHARD_BYTES]
            yield index, block
            index += 1
    if collected:
        yield index, bytes(collected)
        index += 1
    if index != block_count:
        raise PermanentObjectStoreError("Baidu upload stream shard count differs between walks")
