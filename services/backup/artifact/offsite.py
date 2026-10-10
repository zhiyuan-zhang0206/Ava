# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

"""Off-site publication of a finished daily dump to Aliyun OSS.

`publish` uploads one encrypted ``.dump.enc`` iff absent as ``<root>/<name>``
and logs the verified ACK; a missing or failed publish never costs the local
artifact. The only backend is OSS (``AVA_BACKUP_OFFSITE_ENDPOINT``,
``AVA_BACKUP_OFFSITE_BUCKET`` and ``AVA_BACKUP_OFFSITE_CREDENTIALS_FILE``); a home
that has not set all three skips the leg with one INFO line, because an
unconfigured home is a supported state.

The upload is a multipart upload whose integrity is proven twice: every part
carries a ``Content-MD5`` the server verifies, and the completed object's
ETag must equal the MD5-of-part-MD5s chain built from the server-returned
part ETags (the size and metadata must match too).

- iff-absent is server-enforced with ``x-oss-forbid-overwrite`` on
  CompleteMultipartUpload, NOT on InitiateMultipartUpload: init with the
  header on an occupied name fails at once (409 FileAlreadyExists), which
  would kill the adopt-after-crash retry before any part is streamed. A name
  that is already occupied at complete is adopted only when its size, ETag
  chain and metadata prove it the same content.
- Deployment constraint: a versioning-enabled (or suspended) bucket silently
  IGNORES ``x-oss-forbid-overwrite``, so iff-absent degrades to
  check-then-write. The bucket must stay versioning-off.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import oss2
from oss2.models import PartInfo

from services.backup.artifact.names import REMOTE_ROOT

_log = logging.getLogger(__name__)

PART_SIZE = 32 * 1024 * 1024
"""Multipart part size: OSS parts (except the last) must be >= 100 KiB, and 32 MiB
keeps a 20 GB object at 640 parts."""

_FORBID_HEADER = {"x-oss-forbid-overwrite": "true"}
_META_PREFIX = "x-oss-meta-"
_METADATA = {"ava-artifact-kind": "logical-backup"}
_CONNECT_TIMEOUT_S = 300


class OffsiteError(RuntimeError):
    """An off-site publish failed or found a different object under its name."""


@dataclass(frozen=True)
class OffsiteAck:
    """The store-verified identity of one published object."""

    object_name: str
    size: int
    pin_token: str
    md5: str
    created: bool


class _Etagged(Protocol):
    etag: str | None


class _InitResult(Protocol):
    upload_id: str | None


class _HeadResult(Protocol):
    etag: str | None
    content_length: int | None
    headers: Mapping[str, str]


class OssBucket(Protocol):
    """The slice of `oss2.Bucket` a publish uses; tests inject a fake through it."""

    def head_object(self, key: str) -> _HeadResult: ...

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
    ) -> _Etagged: ...

    def complete_multipart_upload(
        self,
        key: str,
        upload_id: str,
        parts: Sequence[PartInfo],
        headers: Mapping[str, str] | None = None,
    ) -> _Etagged: ...

    def abort_multipart_upload(self, key: str, upload_id: str) -> None: ...


@dataclass(frozen=True)
class OssTarget:
    """The configured destination: non-secret endpoint and bucket, secret file path."""

    endpoint: str
    bucket: str
    credentials_file: Path


def configured_target(
    *,
    endpoint_reader: Callable[[], str],
    bucket_reader: Callable[[], str],
    credentials_file_reader: Callable[[], Path | None],
) -> OssTarget | None:
    """The configured OSS destination, or None (one INFO line says why)."""
    keys = {
        "AVA_BACKUP_OFFSITE_ENDPOINT": endpoint_reader(),
        "AVA_BACKUP_OFFSITE_BUCKET": bucket_reader(),
        "AVA_BACKUP_OFFSITE_CREDENTIALS_FILE": credentials_file_reader(),
    }
    unset = [key for key, value in keys.items() if not value]
    credentials_file = credentials_file_reader()
    if unset or credentials_file is None:
        _log.info("[backup] off-site publish skipped: %s unset", ", ".join(unset))
        return None
    return OssTarget(endpoint_reader(), bucket_reader(), credentials_file)


def open_bucket(target: OssTarget) -> OssBucket:
    """Open the OSS bucket with the RAM AccessKey pair in the credentials file."""
    try:
        payload: object = json.loads(target.credentials_file.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("OSS credentials file is not readable JSON") from exc
    if not isinstance(payload, dict):
        raise TypeError("OSS credentials payload must be an object")
    raw = cast(dict[str, object], payload)
    key_id, secret = raw.get("access_key_id"), raw.get("access_key_secret")
    if not isinstance(key_id, str) or not isinstance(secret, str) or not key_id or not secret:
        raise ValueError(
            "OSS credentials must carry a non-empty access_key_id and access_key_secret"
        )
    bucket = oss2.Bucket(
        oss2.Auth(key_id, secret),
        target.endpoint.rstrip("/"),
        target.bucket,
        connect_timeout=_CONNECT_TIMEOUT_S,
    )
    return cast(OssBucket, bucket)


def publish(
    artifact: Path,
    *,
    endpoint_reader: Callable[[], str],
    bucket_reader: Callable[[], str],
    credentials_file_reader: Callable[[], Path | None],
    root: str = REMOTE_ROOT,
    bucket: OssBucket | None = None,
) -> str | None:
    """Best-effort publish of `artifact` as ``<root>/<name>``; the object name or None.

    An unconfigured destination skips with one INFO line. An unusable
    credentials file or a failed upload logs the exception and retains the
    local artifact; neither raises, so the dump that produced `artifact`
    stays good. `bucket` replaces the configured destination (tests).
    """
    if bucket is None:
        target = configured_target(
            endpoint_reader=endpoint_reader,
            bucket_reader=bucket_reader,
            credentials_file_reader=credentials_file_reader,
        )
        if target is None:
            return None
        try:
            bucket = open_bucket(target)
        except Exception:
            _log.exception("[backup] off-site store unavailable; local artifact retained")
            return None
    object_name = f"{root}/{artifact.name}"
    try:
        ack = put_if_absent(bucket, artifact, object_name, _METADATA)
    except Exception:
        _log.exception(
            "[backup] off-site publish of %s failed; local artifact retained", object_name
        )
        return None
    _log.info(
        "[backup] off-site published %s (size=%d, pin=%s, checksum=md5:%s)",
        object_name,
        ack.size,
        ack.pin_token,
        ack.md5,
    )
    return object_name


def put_if_absent(
    bucket: OssBucket, path: Path, object_name: str, metadata: Mapping[str, str]
) -> OffsiteAck:
    """Upload `path` as `object_name` iff absent, or adopt an identical object."""
    size = path.stat().st_size
    if size <= 0:
        raise OffsiteError("off-site upload requires a non-empty artifact")
    upload_id: str | None = None
    part_etags: list[str] = []
    whole = hashlib.md5()  # noqa: S324 — OSS content digest
    try:
        # Deliberately NO forbid-overwrite on init: see the module docstring.
        init = bucket.init_multipart_upload(object_name, headers=_metadata_headers(metadata))
        upload_id = init.upload_id
        if not upload_id:
            raise OffsiteError("OSS multipart upload omitted its upload id")
        with path.open("rb") as source:
            while chunk := source.read(PART_SIZE):
                whole.update(chunk)
                part_etags.append(_upload_part(bucket, object_name, upload_id, chunk, part_etags))
        if not part_etags:
            raise OffsiteError("OSS upload produced no parts")
        parts = [PartInfo(number, etag) for number, etag in enumerate(part_etags, 1)]
        bucket.complete_multipart_upload(object_name, upload_id, parts, headers=_FORBID_HEADER)
        upload_id = None
        return _verified(
            bucket, object_name, size, whole.hexdigest(), part_etags, metadata, created=True
        )
    except oss2.exceptions.OssError as exc:
        if _is_file_exists(exc):
            return _verified(
                bucket, object_name, size, whole.hexdigest(), part_etags, metadata, created=False
            )
        raise OffsiteError(f"OSS upload failed: {exc}") from exc
    finally:
        if upload_id is not None:
            with suppress(oss2.exceptions.ServerError):
                bucket.abort_multipart_upload(object_name, upload_id)


def _upload_part(
    bucket: OssBucket, object_name: str, upload_id: str, data: bytes, uploaded: Sequence[str]
) -> str:
    digest = hashlib.md5(data).hexdigest()  # noqa: S324
    try:
        result = bucket.upload_part(
            object_name,
            upload_id,
            len(uploaded) + 1,
            data,
            headers={"Content-MD5": base64.b64encode(bytes.fromhex(digest)).decode("ascii")},
        )
    except oss2.exceptions.OssError as exc:
        raise OffsiteError(f"OSS part upload failed: {exc}") from exc
    etag = _normalize_etag(result.etag)
    if not etag or etag.lower() != digest:
        raise OffsiteError("OSS part ETag does not match its content MD5")
    return etag


def _verified(
    bucket: OssBucket,
    object_name: str,
    size: int,
    whole_md5: str,
    part_etags: Sequence[str],
    metadata: Mapping[str, str],
    *,
    created: bool,
) -> OffsiteAck:
    """The object under `object_name` proven to be exactly the uploaded parts.

    After a completion it must be our own upload; after FileAlreadyExists it
    is adopted only when it is the same content (a crashed or concurrent run
    of this publisher left it).
    """
    try:
        head = bucket.head_object(object_name)
    except oss2.exceptions.OssError as exc:
        raise OffsiteError(f"OSS verification failed: {exc}") from exc
    etag = _normalize_etag(head.etag)
    if not etag or head.content_length is None:
        raise OffsiteError("OSS object omitted its verification properties")
    chain = hashlib.md5("".join(part_etags).encode()).hexdigest()  # noqa: S324
    if (
        int(head.content_length) != size
        or etag.lower() != f"{chain}-{len(part_etags)}"
        or _user_metadata(head.headers) != dict(metadata)
    ):
        raise OffsiteError("immutable OSS object differs from the local artifact")
    return OffsiteAck(object_name, size, etag, whole_md5, created)


def _normalize_etag(etag: str | None) -> str:
    """Strip the HTTP quoting OSS puts around ETags; keep the server's case."""
    return etag.strip().strip('"') if etag else ""


def _user_metadata(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key[len(_META_PREFIX) :]: value
        for key, value in headers.items()
        if key.startswith(_META_PREFIX)
    }


def _metadata_headers(metadata: Mapping[str, str]) -> dict[str, str]:
    return {f"{_META_PREFIX}{key}": value for key, value in metadata.items()}


def _is_file_exists(exc: oss2.exceptions.OssError) -> bool:
    return isinstance(exc, oss2.exceptions.ServerError) and str(exc.code) == "FileAlreadyExists"
