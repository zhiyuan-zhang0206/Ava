# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

"""Shared fixtures for the Aliyun OSS adapter tests.

Not a test module: pytest never collects this file. The fake lives at the
transport boundary the adapters narrow to (``OSSBucketOps``), so the role
tests exercise one honest in-memory OSS backend: per-part MD5 verification,
the forbid-overwrite precondition, the deterministic multipart ETag chain,
and If-Match pinned reads.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import oss2
from oss2.models import PartInfo

from services.pitr.oss_inventory import OSSRetentionInventoryReader
from services.pitr.oss_publish_store import OSSProtectedManifestPublisher
from services.pitr.oss_restore_store import OSSGenerationPinnedObjectReader
from services.pitr.oss_store import OSSObjectStore

PREFIX = "ava-pitr"
WA_OBJECT = f"{PREFIX}/wal/00000001/000000010000000000000001.enc"


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324 — OSS fixture digest


def _uppercase(data: bytes) -> str:
    return _md5(data).upper()


def _meta(headers: Mapping[str, str] | None) -> dict[str, str]:
    return {
        key[len("x-oss-meta-") :]: value
        for key, value in (headers or {}).items()
        if key.startswith("x-oss-meta-")
    }


def _forbid(headers: Mapping[str, str] | None) -> bool:
    return (headers or {}).get("x-oss-forbid-overwrite") == "true"


def _server_error(status: int, code: str, message: str) -> oss2.exceptions.ServerError:
    return oss2.exceptions.ServerError(status, {}, b"", {"Code": code, "Message": message})


class HeadResult:
    etag: str | None
    content_length: int | None
    headers: Mapping[str, str]

    def __init__(self, record: dict[str, Any]) -> None:
        self.etag = record["etag"]
        self.content_length = record["size"]
        self.headers = {"etag": record["etag"], **_meta_headers(record["metadata"])}


class ReadResult:
    etag: str | None
    content_length: int | None
    headers: Mapping[str, str]

    def __init__(self, record: dict[str, Any]) -> None:
        self.etag = record["etag"]
        self.content_length = record["size"]
        self.headers = {"etag": record["etag"], **_meta_headers(record["metadata"])}
        self._stream = BytesIO(record["data"])

    def read(self, amt: int | None = None) -> bytes:
        return self._stream.read(amt) if amt is not None else self._stream.read()

    def close(self) -> None:
        self._stream.close()


class PutResult:
    etag: str | None

    def __init__(self, etag: str) -> None:
        self.etag = etag


class InitResult:
    upload_id: str | None

    def __init__(self, upload_id: str) -> None:
        self.upload_id = upload_id


class PartResult:
    etag: str | None
    size: int | None

    def __init__(self, etag: str, size: int) -> None:
        self.etag = etag
        self.size = size


class CompleteResult:
    etag: str | None

    def __init__(self, etag: str) -> None:
        self.etag = etag


class ListEntry:
    key: str
    size: int | None
    etag: str | None

    def __init__(self, key: str, record: dict[str, Any]) -> None:
        self.key = key
        self.size = record["size"]
        self.etag = record["etag"]


class ListPage:
    object_list: Sequence[ListEntry]
    is_truncated: bool
    next_marker: str

    def __init__(self, entries: list[ListEntry], truncated: bool, marker: str) -> None:
        self.object_list = entries
        self.is_truncated = truncated
        self.next_marker = marker


def _meta_headers(metadata: Mapping[str, str]) -> dict[str, str]:
    return {f"x-oss-meta-{key}": value for key, value in metadata.items()}


class FakeOssBucket:
    """Stateful in-memory OSS control plane narrowing ``OSSBucketOps``."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self._parts: dict[str, dict[int, bytes]] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._next_upload_id = 1
        # Error-injection hooks for the taxonomy / fail-closed tests.
        self.corrupt_part_etags = False
        self.corrupt_complete_etag = False
        self.head_error: tuple[int, str] | None = None
        self.request_error = False

    def seed(
        self,
        key: str,
        *,
        data: bytes,
        metadata: dict[str, str] | None = None,
        multipart: bool = False,
    ) -> str:
        etag = _multipart_etag_of(data, 1) if multipart else _uppercase(data)
        self.files[key] = {
            "etag": etag,
            "size": len(data),
            "metadata": dict(metadata or {}),
            "data": data,
            "type": "Multipart" if multipart else "Normal",
        }
        return etag

    # ── OSSBucketOps ──

    def put_object(
        self, key: str, data: object, headers: Mapping[str, str] | None = None
    ) -> PutResult:
        content = data if isinstance(data, bytes) else cast(Any, data).read()
        assert isinstance(content, bytes)
        digest = _md5(content)
        expected = headers.get("Content-MD5") if headers else None
        if expected is not None and base64.b64encode(bytes.fromhex(digest)).decode() != expected:
            raise _server_error(400, "InvalidDigest", "Content-MD5 does not match the body")
        if key in self.files:
            if _forbid(headers):
                raise _server_error(409, "FileAlreadyExists", "forbidden to overwrite")
            record = self.files[key]
            if record["data"] != content or _meta(headers) != record["metadata"]:
                parent_etag = record["etag"]
                etag = (
                    _uppercase(content)
                    if "-" not in parent_etag
                    else _multipart_etag_of(content, 1)
                )
                self.files[key] = {
                    "etag": etag,
                    "size": len(content),
                    "metadata": _meta(headers),
                    "data": content,
                    "type": "Normal",
                }
                return PutResult(etag)
            return PutResult(record["etag"])
        etag = _uppercase(content)
        self.files[key] = {
            "etag": etag,
            "size": len(content),
            "metadata": _meta(headers),
            "data": content,
            "type": "Normal",
        }
        return PutResult(etag)

    def head_object(self, key: str, headers: Mapping[str, str] | None = None) -> HeadResult:
        if self.request_error:
            raise oss2.exceptions.RequestError(OSError("injected transport failure"))
        if self.head_error is not None:
            status, code = self.head_error
            raise _server_error(status, code, f"injected {code}")
        if key not in self.files:
            raise _server_error(404, "NoSuchKey", "the object does not exist")
        return HeadResult(self.files[key])

    def get_object(self, key: str, headers: Mapping[str, str] | None = None) -> ReadResult:
        if key not in self.files:
            raise _server_error(404, "NoSuchKey", "the object does not exist")
        record = self.files[key]
        if_match = (headers or {}).get("If-Match")
        if if_match is not None and if_match.strip('"') != record["etag"]:
            raise _server_error(412, "PreconditionFailed", "If-Match differs")
        return ReadResult(record)

    def list_objects(
        self, prefix: str = "", marker: str = "", max_keys: int = 100, **_: object
    ) -> ListPage:
        keys = sorted(key for key in self.files if key.startswith(prefix) and key > marker)
        entries = [ListEntry(key, self.files[key]) for key in keys[:max_keys]]
        truncated = len(keys) > max_keys
        return ListPage(entries, truncated, entries[-1].key if entries and truncated else "")

    def init_multipart_upload(
        self, key: str, headers: Mapping[str, str] | None = None
    ) -> InitResult:
        upload_id = f"up{self._next_upload_id}"
        self._next_upload_id += 1
        self._pending[upload_id] = {"key": key, "metadata": _meta(headers)}
        self._parts[upload_id] = {}
        return InitResult(upload_id)

    def upload_part(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        data: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> PartResult:
        digest = _md5(data)
        expected = headers.get("Content-MD5") if headers else None
        if expected is not None and base64.b64encode(bytes.fromhex(digest)).decode() != expected:
            raise _server_error(400, "InvalidDigest", "part MD5 does not match the body")
        if self.corrupt_part_etags:
            return PartResult("Z" * 32, len(data))
        self._parts.setdefault(upload_id, {})[part_number] = data
        return PartResult(_uppercase(data), len(data))

    def complete_multipart_upload(
        self,
        key: str,
        upload_id: str,
        parts: Sequence[PartInfo],
        headers: Mapping[str, str] | None = None,
    ) -> CompleteResult:
        if key in self.files and _forbid(headers):
            raise _server_error(409, "FileAlreadyExists", "forbidden to overwrite")
        part_map = self._parts.get(upload_id, {})
        content = b"".join(part_map[part.part_number] for part in parts)
        etags = [part.etag for part in parts]
        token = "".join(etags)
        etag = f"{_md5(token.encode()).upper()}-{len(parts)}"
        if self.corrupt_complete_etag:
            etag = etag[:-3] + "999"
        self.files[key] = {
            "etag": etag,
            "size": len(content),
            "metadata": dict(self._pending.get(upload_id, {}).get("metadata", {})),
            "data": content,
            "type": "Multipart",
        }
        self._parts.pop(upload_id, None)
        self._pending.pop(upload_id, None)
        return CompleteResult(etag)

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        self._parts.pop(upload_id, None)
        self._pending.pop(upload_id, None)


def _multipart_etag_of(data: bytes, part_count: int) -> str:
    etags = [_uppercase(data)]
    return f"{_md5(''.join(etags).encode()).upper()}-{part_count}"


def make_store(fake: FakeOssBucket) -> OSSObjectStore:
    # The fake narrows OSSBucketOps at the same seam the GCS contract tests
    # use cast(Any, ...) — protocol conformance is enforced by the role tests.
    return OSSObjectStore.from_bucket(cast(Any, fake))


def make_reader(fake: FakeOssBucket) -> OSSGenerationPinnedObjectReader:
    return OSSGenerationPinnedObjectReader.from_store(make_store(fake))


def make_inventory(fake: FakeOssBucket, *, prefix: str = PREFIX) -> OSSRetentionInventoryReader:
    return OSSRetentionInventoryReader.from_store(make_store(fake), prefix=prefix)


def make_publisher(fake: FakeOssBucket) -> OSSProtectedManifestPublisher:
    return OSSProtectedManifestPublisher.from_store(make_store(fake))


class ChunkSource:
    """Deterministic restartable ciphertext for streaming-role tests."""

    def __init__(self, chunks: list[bytes], *, size: int | None = None) -> None:
        self._chunks = chunks
        self._size = size if size is not None else sum(len(chunk) for chunk in chunks)
        self.walks = 0

    @property
    def ciphertext_size(self) -> int:
        return self._size

    @property
    def ciphertext_crc32c(self) -> str:
        return ""

    def iter_chunks(self) -> Iterator[bytes]:
        self.walks += 1
        yield from self._chunks


def oss_credentials_file(tmp_path: Path, *, key_id: str = "uploader-ak") -> Path:
    path = tmp_path / "oss.json"
    path.write_text(json.dumps({"access_key_id": key_id, "access_key_secret": "secret-value"}))
    path.chmod(0o600)
    return path


def base_ack_metadata(payload: bytes) -> dict[str, str]:
    return {
        "ava-candidate-sha256": "a" * 64,
        "ava-ciphertext-size": str(len(payload)),
        "ava-ciphertext-crc32c": "crc-local",
        "ava-encryption-format": "AVAPITR1",
        "ava-key-id": "prod-v1",
        "ava-packer-version": "1",
    }


def wal_ack_metadata() -> dict[str, str]:
    return {
        "ava-archive-name": "000000010000000000000001",
        "ava-source-sha256": "b" * 64,
        "ava-source-size": "16",
        "ava-ciphertext-crc32c": "crc-local",
        "ava-encryption-format": "AVAPITR1",
        "ava-key-id": "prod-v1",
    }
