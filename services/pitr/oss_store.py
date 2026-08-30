# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

"""Aliyun OSS adapter for the owned immutable-object boundary.

One class serves the ObjectStore and RestartableStreamingObjectStore roles
internally (the role contracts stay separate at the callers). The adapter
speaks the official ``oss2`` SDK against a RAM AccessKey pair:

- iff-absent is server-enforced with ``x-oss-forbid-overwrite`` on every
  publish verb: PutObject for WAL ciphertexts (small, seekable) and the
  CompleteMultipartUpload for base ciphertexts (large, restartable stream) —
  NOT on InitiateMultipartUpload, because init with the header on an
  existing name fails immediately (409 FileAlreadyExists) and would kill
  the adopt-after-crash retry path before any part is streamed. OSS does
  not implement HTTP conditional headers on PutObject — the OSS-specific
  header is the only primitive, and the "exists" answer is the
  ``FileAlreadyExists`` error (HTTP 409) at the publish verb.
  Deployment constraint: a versioning-enabled (or suspended) bucket
  silently IGNORES ``x-oss-forbid-overwrite`` — if-absent degrades to
  check-then-write. Verify the bucket is versioning-off before enabling
  this backend; the live smoke must pin it.
- pin_token is the object ETag — the pinned-read credential restore drills
  send back as ``If-Match``. WAL objects are single PUTs, so the ETag IS the
  content MD5; base objects are multipart uploads, whose ETag is the
  deterministic MD5-of-part-MD5s chain, and the adapter asserts the
  completed ETag equals the chain built from the server-returned part ETags
  (each part carries a Content-MD5 the server verified). The ACK checksum is
  therefore MD5 for both shapes: server-verified directly for single PUTs,
  server-verified per part plus the ETag-chain proof for multipart.
- metadata: WAL objects carry the caller metadata map verbatim (activation
  compares it exactly); base objects additionally get a sidecar
  ``<object>.ack.json`` because OSS never exposes a whole-object digest for
  multipart uploads — the sidecar lets the retention inventory reproduce
  the exact ACK identity a candidate manifest promised (QA #2157).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol, cast

import oss2
from oss2.models import PartInfo

from services.pitr.base_object_store import RestartableEncryptedSource
from services.pitr.checksums import MD5, ObjectChecksum, digest_file
from services.pitr.object_store import (
    ObjectStoreError,
    PermanentObjectStoreError,
    RemoteObjectAck,
    TransientObjectStoreError,
)

PART_SIZE = 32 * 1024 * 1024
"""Multipart part ceiling: OSS parts (except the last) must be >= 100 KiB;
32 MiB keeps a 20 GB object at 640 parts and threads two walks over a
restartable source cheaply."""

_FORBID_OVERWRITE = "x-oss-forbid-overwrite"
_FORBID_HEADER = {_FORBID_OVERWRITE: "true"}
"""Server-enforced iff-absent: OSS rejects the publish verb with
``FileAlreadyExists`` (HTTP 409) when the object exists."""

_META_PREFIX = "x-oss-meta-"
_MD5_HEX = re.compile(r"^[0-9a-fA-F]{32}$")

_SIDECAR_SUFFIX = ".ack.json"
"""Sidecar object name suffix; the inventory skips these names."""


def _normalize_etag(etag: str | None) -> str:
    """Strip the HTTP quoting OSS puts around ETags; keep the server's case."""
    if not etag:
        return ""
    return etag.strip().strip('"')


def _etag_md5(etag: str) -> str | None:
    """The content MD5 when an ETag is a plain single-put MD5, else None.

    None means the object was published as multipart: its ETag is the
    MD5-of-part-MD5s chain, not a content digest.
    """
    if _MD5_HEX.fullmatch(etag):
        return etag.lower()
    return None


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324 — OSS content digest


def _b64_md5(digest: str) -> str:
    return base64.b64encode(bytes.fromhex(digest)).decode("ascii")


def _user_metadata(headers: Mapping[str, str]) -> dict[str, str]:
    """OSS user metadata arrives as ``x-oss-meta-<key>`` response headers."""
    return {
        key[len(_META_PREFIX) :]: value
        for key, value in headers.items()
        if key.startswith(_META_PREFIX)
    }


def _metadata_headers(metadata: Mapping[str, str]) -> dict[str, str]:
    return {f"{_META_PREFIX}{key}": value for key, value in metadata.items()}


def _multipart_etag(part_etags: Sequence[str]) -> str:
    """OSS's deterministic multipart ETag: MD5 over the concatenated part
    ETags, suffixed with the part count."""
    chain = hashlib.md5("".join(part_etags).encode()).hexdigest()  # noqa: S324
    return f"{chain}-{len(part_etags)}"


def _is_not_found(exc: object) -> bool:
    if not isinstance(exc, oss2.exceptions.ServerError):
        return False
    return bool(str(exc.code) == "NoSuchKey" or exc.status == 404)


def _is_file_exists(exc: object) -> bool:
    if not isinstance(exc, oss2.exceptions.ServerError):
        return False
    return bool(str(exc.code) == "FileAlreadyExists")


def _map_error(operation: str, exc: BaseException) -> ObjectStoreError:
    if isinstance(exc, oss2.exceptions.RequestError) or (
        isinstance(exc, oss2.exceptions.ServerError) and (exc.status >= 500 or exc.status == 429)
    ):
        return TransientObjectStoreError(f"{operation} temporarily failed: {exc}")
    if isinstance(exc, oss2.exceptions.ServerError):
        return PermanentObjectStoreError(f"{operation} was rejected: {exc}")
    return PermanentObjectStoreError(f"{operation} failed: {exc}")


# ── transport protocol (narrowed for fake / contract tests) ──


class _HeadResult(Protocol):
    etag: str | None
    content_length: int | None
    headers: Mapping[str, str]


class _PutResult(Protocol):
    etag: str | None


class _InitResult(Protocol):
    upload_id: str | None


class _PartResult(Protocol):
    etag: str | None
    size: int | None


class _CompleteResult(Protocol):
    etag: str | None


class _ReadResult(Protocol):
    etag: str | None
    content_length: int | None
    headers: Mapping[str, str]

    def read(self, amt: int | None = None) -> bytes: ...

    def close(self) -> None: ...


class _ListObject(Protocol):
    key: str
    size: int | None
    etag: str | None


class _ListPage(Protocol):
    is_truncated: bool
    next_marker: str
    object_list: Sequence[_ListObject]


class OSSBucketOps(Protocol):
    def put_object(
        self, key: str, data: object, headers: Mapping[str, str] | None = None
    ) -> _PutResult: ...

    def head_object(self, key: str, headers: Mapping[str, str] | None = None) -> _HeadResult: ...

    def get_object(self, key: str, headers: Mapping[str, str] | None = None) -> _ReadResult: ...

    def list_objects(
        self, prefix: str = "", marker: str = "", max_keys: int = 100, **_: object
    ) -> _ListPage: ...

    def init_multipart_upload(
        self, key: str, headers: Mapping[str, str] | None = None
    ) -> _InitResult: ...

    def upload_part(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        data: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> _PartResult: ...

    def complete_multipart_upload(
        self,
        key: str,
        upload_id: str,
        parts: Sequence[PartInfo],
        headers: Mapping[str, str] | None = None,
    ) -> _CompleteResult: ...

    def abort_multipart_upload(self, key: str, upload_id: str) -> None: ...


class _PartRecorder:
    def __init__(self) -> None:
        self._parts: list[tuple[int, str]] = []

    def add(self, number: int, etag: str) -> None:
        self._parts.append((number, etag))

    def etags(self) -> list[str]:
        return [etag for _number, etag in self._parts]

    def oss_parts(self) -> list[PartInfo]:
        return [PartInfo(number, etag) for number, etag in self._parts]

    def __bool__(self) -> bool:
        return bool(self._parts)


class OSSObjectStore:
    """One bucket-scoped adapter; credentials never enter process argv."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        credentials_file: str | Path,
        timeout_seconds: float = 300.0,
    ) -> None:
        from services.pitr.oss_credentials import open_oss_bucket

        self._bucket = cast(
            OSSBucketOps,
            open_oss_bucket(
                endpoint=endpoint,
                bucket=bucket,
                credentials_file=credentials_file,
                timeout_seconds=timeout_seconds,
            ),
        )

    @classmethod
    def from_bucket(cls, bucket: OSSBucketOps) -> OSSObjectStore:
        """Construct around an injected transport for contract tests."""
        instance = cls.__new__(cls)
        instance._bucket = bucket
        return instance

    # ── shared verification helpers ──

    def read_sidecar(self, object_name: str) -> dict[str, Any] | None:
        """Public read-back of an object's sidecar (inventory + stat use)."""
        sidecar_name = f"{object_name}{_SIDECAR_SUFFIX}"
        try:
            obj = self._bucket.get_object(sidecar_name)
        except oss2.exceptions.OssError as exc:
            if _is_not_found(exc):
                return None
            raise _map_error("OSS sidecar lookup", exc) from exc
        try:
            data = obj.read()
        finally:
            obj.close()
        if obj.content_length is not None and len(data) != int(obj.content_length):
            raise PermanentObjectStoreError("OSS object sidecar is truncated")
        # The sidecar is a single PUT, so its ETag must be the content MD5 —
        # the same integrity check the Baidu reader applies to its sidecar.
        if _normalize_etag(obj.etag).lower() != _md5_hex(data):
            raise PermanentObjectStoreError("OSS object sidecar differs from its ETag")
        try:
            payload = json.loads(data.decode())
        except (ValueError, UnicodeDecodeError) as exc:
            raise PermanentObjectStoreError("OSS object sidecar is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise PermanentObjectStoreError("OSS object sidecar is not an object")
        sidecar = cast(dict[str, Any], payload)
        if sidecar.get("object_name") != object_name:
            raise PermanentObjectStoreError("OSS object sidecar names a different object")
        try:
            ObjectChecksum(str(sidecar["checksum_algo"]), str(sidecar["checksum_value"]))
            int(sidecar["size"])
            str(sidecar["pin_token"])
            dict[str, str](sidecar["metadata"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PermanentObjectStoreError("OSS object sidecar identity is malformed") from exc
        return sidecar

    def _object_checksum(self, object_name: str, etag: str, size: int) -> ObjectChecksum:
        md5 = _etag_md5(etag)
        if md5 is not None:
            return ObjectChecksum(MD5, md5)
        # Multipart objects have no server-exposed content digest; the
        # sidecar carries the exact ACK identity the publisher promised.
        sidecar = self.read_sidecar(object_name)
        if sidecar is None or str(sidecar.get("pin_token")) != etag or int(sidecar["size"]) != size:
            raise PermanentObjectStoreError("immutable OSS object lacks a readable ACK sidecar")
        return ObjectChecksum(str(sidecar["checksum_algo"]), str(sidecar["checksum_value"]))

    def _write_sidecar_if_absent(self, object_name: str, ack: RemoteObjectAck) -> None:
        """Publish ``<object>.ack.json`` iff absent; an existing sidecar must
        carry the identical identity or the object is not adoptable."""
        payload = {
            "object_name": ack.object_name,
            "pin_token": ack.pin_token,
            "size": ack.size,
            "checksum_algo": ack.checksum.algo,
            "checksum_value": ack.checksum.value,
            "metadata": dict(ack.metadata),
        }
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sidecar_name = f"{object_name}{_SIDECAR_SUFFIX}"
        existing = self.read_sidecar(object_name)
        if existing is not None:
            if existing != payload:
                raise PermanentObjectStoreError("immutable OSS object sidecar differs")
            return
        try:
            self._bucket.put_object(
                sidecar_name,
                data,
                headers={
                    **_FORBID_HEADER,
                    "Content-MD5": _b64_md5(_md5_hex(data)),
                    **_metadata_headers({"ava-sidecar-of": object_name}),
                },
            )
        except oss2.exceptions.OssError as exc:
            if _is_file_exists(exc):
                existing = self.read_sidecar(object_name)
                if existing is None or existing != payload:
                    raise PermanentObjectStoreError("immutable OSS object sidecar differs") from exc
                return
            raise _map_error("OSS sidecar publish", exc) from exc
        # Every publish verb must survive the "verify what we just wrote"
        # step: the sidecar is small, so read it back and compare bytes.
        written = self.read_sidecar(object_name)
        if written != payload:
            raise PermanentObjectStoreError("immutable OSS object sidecar differs after publish")

    def get_object(
        self, object_name: str, *, headers: Mapping[str, str] | None = None
    ) -> _ReadResult:
        """Transport passthrough for viewer roles (restore reader, publisher
        verification, sidecar read-back)."""
        return self._bucket.get_object(object_name, headers=headers)

    def put_object(
        self, object_name: str, data: bytes, *, headers: Mapping[str, str] | None = None
    ) -> object:
        """Transport passthrough for the protected-manifest publisher."""
        return self._bucket.put_object(object_name, data, headers=headers)

    def list_objects(self, prefix: str) -> list[_ListObject]:
        """Paginate a prefix listing (inventory uses this; no write verb)."""
        items: list[_ListObject] = []
        marker = ""
        while True:
            page = self._bucket.list_objects(prefix=prefix, marker=marker, max_keys=1000)
            items.extend(page.object_list)
            if not page.is_truncated or not page.next_marker:
                return items
            marker = page.next_marker

    # ── ObjectStore role ──

    def stat(self, object_name: str) -> RemoteObjectAck | None:
        """Re-observe an object: ETag (pin), size, checksum, metadata."""
        try:
            head = self._bucket.head_object(object_name)
        except oss2.exceptions.OssError as exc:
            if _is_not_found(exc):
                return None
            raise _map_error("OSS stat", exc) from exc
        etag = _normalize_etag(head.etag)
        if not etag or head.content_length is None:
            raise TransientObjectStoreError("OSS object omitted verification properties")
        size = int(head.content_length)
        return RemoteObjectAck(
            object_name=object_name,
            pin_token=etag,
            size=size,
            checksum=self._object_checksum(object_name, etag, size),
            metadata=_user_metadata(head.headers),
            created=False,
        )

    def put_wal_ciphertext_if_absent(
        self,
        path: Path,
        object_name: str,
        metadata: Mapping[str, str],
    ) -> RemoteObjectAck:
        """Publish one bounded WAL staging file with the server-enforced
        forbid-overwrite precondition; the file must be real and seekable
        (the shared ObjectStore contract — base backups use the separate
        restartable-stream boundary)."""
        size = path.stat().st_size
        digest = digest_file(MD5, str(path))
        try:
            with path.open("rb") as source:
                self._bucket.put_object(
                    object_name,
                    source,
                    headers={
                        **_FORBID_HEADER,
                        "Content-MD5": _b64_md5(digest),
                        **_metadata_headers(metadata),
                    },
                )
        except oss2.exceptions.OssError as exc:
            if _is_file_exists(exc):
                return self._adopt_existing(
                    object_name, size, ObjectChecksum(MD5, digest), metadata
                )
            raise _map_error("OSS WAL publish", exc) from exc
        return self._verify_published(
            object_name, size, ObjectChecksum(MD5, digest), metadata, created=True
        )

    def _verify_published(
        self,
        object_name: str,
        size: int,
        checksum: ObjectChecksum,
        metadata: Mapping[str, str],
        *,
        created: bool,
    ) -> RemoteObjectAck:
        ack = self.stat(object_name)
        if ack is None:
            raise TransientObjectStoreError("OSS publish raced with a missing object")
        if (
            ack.object_name != object_name
            or not ack.pin_token
            or ack.size != size
            or ack.checksum != checksum
            or dict(ack.metadata) != dict(metadata)
        ):
            raise PermanentObjectStoreError("immutable OSS object differs from the local archive")
        return RemoteObjectAck(
            object_name=ack.object_name,
            pin_token=ack.pin_token,
            size=ack.size,
            checksum=ack.checksum,
            metadata=ack.metadata,
            created=created,
        )

    def _adopt_existing(
        self, object_name: str, size: int, checksum: ObjectChecksum, metadata: Mapping[str, str]
    ) -> RemoteObjectAck:
        return self._verify_published(object_name, size, checksum, metadata, created=False)

    # ── RestartableStreamingObjectStore role ──

    def put_base_if_absent(
        self,
        *,
        source: RestartableEncryptedSource,
        object_name: str,
        metadata: Mapping[str, str],
        cancelled: Callable[[], bool] = lambda: False,
    ) -> RemoteObjectAck:
        size = source.ciphertext_size
        if size <= 0:
            raise PermanentObjectStoreError("OSS base upload requires a positive ciphertext size")
        if cancelled():
            raise RuntimeError("OSS base upload cancelled before publication")
        upload_id: str | None = None
        parts = _PartRecorder()
        whole = hashlib.md5()  # noqa: S324
        try:
            init = self._bucket.init_multipart_upload(
                object_name,
                # Deliberately NO forbid-overwrite here: init with it fails
                # immediately when the name is occupied, which would make the
                # adopt-after-crash retry (FileAlreadyExists at complete -> ETag
                # chain adoption) impossible. Iff-absent is enforced at complete,
                # where the object is actually created.
                headers=_metadata_headers(metadata),
            )
            upload_id = init.upload_id
            if not upload_id:
                raise PermanentObjectStoreError("OSS multipart upload omitted its upload id")
            buffer = bytearray()
            for chunk in source.iter_chunks():
                if cancelled():
                    raise RuntimeError("OSS base upload cancelled at chunk boundary")
                whole.update(chunk)
                buffer.extend(chunk)
                while len(buffer) >= PART_SIZE:
                    self._upload_part(object_name, upload_id, bytes(buffer[:PART_SIZE]), parts)
                    del buffer[:PART_SIZE]
            if buffer:
                self._upload_part(object_name, upload_id, bytes(buffer), parts)
            if not parts:
                raise PermanentObjectStoreError("OSS base upload produced no parts")
            if cancelled():
                raise RuntimeError("OSS base upload cancelled before completion")
            self._bucket.complete_multipart_upload(
                object_name, upload_id, parts.oss_parts(), headers=_FORBID_HEADER
            )
            upload_id = None
            return self._finalize_base(
                object_name, size, whole.hexdigest(), parts.etags(), metadata, cancelled
            )
        except oss2.exceptions.OssError as exc:
            if _is_file_exists(exc):
                return self._adopt_base(
                    object_name, size, whole.hexdigest(), parts.etags(), metadata
                )
            raise _map_error("OSS base upload", exc) from exc
        finally:
            if upload_id is not None:
                with suppress(oss2.exceptions.ServerError):
                    self._bucket.abort_multipart_upload(object_name, upload_id)

    def _upload_part(
        self, object_name: str, upload_id: str, data: bytes, parts: _PartRecorder
    ) -> None:
        try:
            result = self._bucket.upload_part(
                object_name,
                upload_id,
                len(parts.etags()) + 1,
                data,
                headers={"Content-MD5": _b64_md5(_md5_hex(data))},
            )
        except oss2.exceptions.OssError as exc:
            raise _map_error("OSS shard upload", exc) from exc
        etag = _normalize_etag(result.etag)
        if not etag or etag.lower() != _md5_hex(data):
            raise PermanentObjectStoreError("OSS shard ETag does not match its content MD5")
        parts.add(len(parts.etags()) + 1, etag)

    def _finalize_base(
        self,
        object_name: str,
        size: int,
        whole_md5: str,
        part_etags: list[str],
        metadata: Mapping[str, str],
        cancelled: Callable[[], bool],
    ) -> RemoteObjectAck:
        """Verify the completed multipart object against the uploaded parts,
        then publish its sidecar. The ETag chain proves the object is the
        concatenation of exactly the server-verified part bodies."""
        head = self._completion_head(object_name)
        etag = _normalize_etag(head.etag)
        if not etag or head.content_length is None or int(head.content_length) != size:
            raise PermanentObjectStoreError("immutable OSS base object differs after completion")
        if etag.lower() != _multipart_etag(part_etags).lower():
            raise PermanentObjectStoreError("OSS multipart ETag does not match the uploaded parts")
        metadata_map = dict(metadata)
        if _user_metadata(head.headers) != metadata_map:
            raise PermanentObjectStoreError("immutable OSS base object metadata differs")
        ack = RemoteObjectAck(
            object_name=object_name,
            pin_token=etag,
            size=size,
            checksum=ObjectChecksum(MD5, whole_md5),
            metadata=metadata_map,
            created=True,
        )
        self._write_sidecar_if_absent(object_name, ack)
        if cancelled():
            raise RuntimeError("OSS base upload lost ownership before local publication")
        return ack

    def _adopt_base(
        self,
        object_name: str,
        size: int,
        whole_md5: str,
        part_etags: list[str],
        metadata: Mapping[str, str],
    ) -> RemoteObjectAck:
        """A concurrent or crashed previous run left the canonical name
        occupied. Adopt it only when its identity proves the same content:
        the ETag matches our own part chain (identical parts), or the
        sidecar records the same checksum and metadata."""
        head = self._completion_head(object_name)
        etag = _normalize_etag(head.etag)
        if not etag or head.content_length is None:
            raise TransientObjectStoreError("OSS base precondition raced with a missing object")
        size_ok = int(head.content_length) == size
        chain_ok = etag.lower() == _multipart_etag(part_etags).lower()
        metadata_map = dict(metadata)
        metadata_ok = _user_metadata(head.headers) == metadata_map
        checksum = ObjectChecksum(MD5, whole_md5)
        sidecar = None
        if not (size_ok and chain_ok and metadata_ok):
            sidecar = self.read_sidecar(object_name)
            if (
                sidecar is None
                or int(sidecar["size"]) != size
                or str(sidecar["pin_token"]) != etag
                or ObjectChecksum(str(sidecar["checksum_algo"]), str(sidecar["checksum_value"]))
                != checksum
                or dict(cast(Mapping[str, str], sidecar["metadata"])) != metadata_map
            ):
                raise PermanentObjectStoreError(
                    "immutable OSS base object differs from the local archive"
                )
        ack = RemoteObjectAck(
            object_name=object_name,
            pin_token=etag,
            size=size,
            checksum=checksum,
            metadata=metadata_map,
            created=False,
        )
        if sidecar is None:
            self._write_sidecar_if_absent(object_name, ack)
        return ack

    def _completion_head(self, object_name: str) -> _HeadResult:
        try:
            return self._bucket.head_object(object_name)
        except oss2.exceptions.OssError as exc:
            if _is_not_found(exc):
                raise TransientObjectStoreError(
                    "OSS precondition raced with a missing object"
                ) from None
            raise _map_error("OSS base verification", exc) from exc
