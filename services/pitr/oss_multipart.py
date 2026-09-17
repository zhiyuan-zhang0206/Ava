# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

"""Operator surface over OSS in-progress multipart uploads (orphan shards).

An interrupted publish leaves an incomplete multipart upload behind: the
parts it already streamed bill for storage, they are invisible to the object
listing, and the ordinary publish vocabulary cannot remove them -- only
``AbortMultipartUpload`` can. Two channels collect them:

- the bucket lifecycle rule (``pitr-expire-90d``: fragments aborted after 7
  days, whole bucket) is the standing channel -- orphans self-heal without
  any operator action;
- this module is the explicit channel: a read-only inventory and a
  single-upload abort for the cases that cannot wait out the lifecycle
  window.

The abort can only ever remove an *incomplete* upload: the upload id stops
existing the moment an upload completes, so a late abort answers
NoSuchUpload and nothing user-visible is touched. There is deliberately no
bulk or prefix abort verb.

The uploader identity is the natural owner of this surface: the publish path
already aborts its own strays best-effort (``OSSObjectStore.put_base_if_absent``)
the moment its credential carries ``oss:AbortMultipartUpload``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

import oss2

from services.pitr.oss_store import map_oss_error

# OSS multipart list pages (uploads / parts), 1000 per call — the API's page
# size (task #3696 exception inventory).
_UPLOAD_PAGE_SIZE = 1000
_PARTS_PAGE_SIZE = 1000


@dataclass(frozen=True)
class IncompleteUpload:
    """One in-progress multipart upload as the operator surface sees it."""

    key: str
    upload_id: str
    initiated: int
    """Unix seconds the upload was initiated (OSS ``initiation_date``)."""

    part_count: int
    size_bytes: int


class AbortOutcome(StrEnum):
    """The result of one explicit single-upload abort."""

    ABORTED = "aborted"
    NOT_FOUND = "not_found"
    """No such incomplete upload: it completed, was aborted, or never
    existed -- nothing user-visible was touched (idempotent no-op)."""


class _UploadItem(Protocol):
    key: str
    upload_id: str
    initiation_date: int


class _UploadsPage(Protocol):
    upload_list: list[_UploadItem]
    is_truncated: bool
    next_key_marker: str
    next_upload_id_marker: str


class _PartItem(Protocol):
    size: int | None


class _PartsPage(Protocol):
    parts: list[_PartItem]
    is_truncated: bool
    next_marker: str


class _MultipartBucketOps(Protocol):
    """The narrow transport the surface consumes (fake-injectable)."""

    def list_multipart_uploads(
        self,
        prefix: str = "",
        key_marker: str = "",
        upload_id_marker: str = "",
        max_uploads: int = 1000,
    ) -> _UploadsPage: ...

    def list_parts(
        self, key: str, upload_id: str, marker: str = "", max_parts: int = 1000
    ) -> _PartsPage: ...

    def abort_multipart_upload(self, key: str, upload_id: str) -> object: ...


def _is_no_such_upload(exc: object) -> bool:
    if not isinstance(exc, oss2.exceptions.ServerError):
        return False
    return bool(str(exc.code) == "NoSuchUpload" or exc.status == 404)


class OSSMultipartUploads:
    """Read-only inventory and explicit single-upload abort for OSS."""

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
            _MultipartBucketOps,
            open_oss_bucket(
                endpoint=endpoint,
                bucket=bucket,
                credentials_file=credentials_file,
                timeout_seconds=timeout_seconds,
            ),
        )

    @classmethod
    def from_bucket(cls, bucket: _MultipartBucketOps) -> OSSMultipartUploads:
        """Construct around an injected transport for contract tests."""

        instance = cls.__new__(cls)
        instance._bucket = bucket
        return instance

    def inventory(self, *, prefix: str = "") -> list[IncompleteUpload]:
        """Every incomplete upload under ``prefix``, oldest first.

        Each upload is sized through ListParts (one extra call per upload).
        An upload that vanishes mid-scan -- completed or aborted
        concurrently -- drops out of the result instead of failing the scan;
        every other read error propagates through the store taxonomy.
        """

        rows: list[IncompleteUpload] = []
        for item in self._uploads(prefix=prefix):
            sized = self._size(item.key, item.upload_id)
            if sized is None:
                continue
            part_count, size_bytes = sized
            rows.append(
                IncompleteUpload(
                    key=item.key,
                    upload_id=item.upload_id,
                    initiated=int(item.initiation_date),
                    part_count=part_count,
                    size_bytes=size_bytes,
                )
            )
        rows.sort(key=lambda row: (row.initiated, row.key))
        return rows

    def find(self, *, key: str, upload_id: str) -> IncompleteUpload | None:
        """The named incomplete upload, or None when it is not one."""

        for item in self._uploads(prefix=key):
            if item.key != key or item.upload_id != upload_id:
                continue
            sized = self._size(key, upload_id)
            if sized is None:
                return None
            part_count, size_bytes = sized
            return IncompleteUpload(
                key=item.key,
                upload_id=item.upload_id,
                initiated=int(item.initiation_date),
                part_count=part_count,
                size_bytes=size_bytes,
            )
        return None

    def abort(self, *, key: str, upload_id: str) -> AbortOutcome:
        """Abort exactly one incomplete upload; NOT_FOUND when it is gone."""

        try:
            self._bucket.abort_multipart_upload(key, upload_id)
        except oss2.exceptions.OssError as exc:
            if _is_no_such_upload(exc):
                return AbortOutcome.NOT_FOUND
            raise map_oss_error("OSS multipart abort", exc) from exc
        return AbortOutcome.ABORTED

    # ── internals ──

    def _uploads(self, *, prefix: str) -> list[_UploadItem]:
        items: list[_UploadItem] = []
        key_marker = ""
        upload_id_marker = ""
        while True:
            try:
                page = self._bucket.list_multipart_uploads(
                    prefix=prefix,
                    key_marker=key_marker,
                    upload_id_marker=upload_id_marker,
                    max_uploads=_UPLOAD_PAGE_SIZE,
                )
            except oss2.exceptions.OssError as exc:
                raise map_oss_error("OSS multipart listing", exc) from exc
            items.extend(page.upload_list)
            if not page.is_truncated:
                return items
            key_marker = page.next_key_marker
            upload_id_marker = page.next_upload_id_marker

    def _size(self, key: str, upload_id: str) -> tuple[int, int] | None:
        """(part_count, size_bytes) for the upload, or None when it vanished."""

        part_count = 0
        size_bytes = 0
        marker = ""
        while True:
            try:
                page = self._bucket.list_parts(
                    key, upload_id, marker=marker, max_parts=_PARTS_PAGE_SIZE
                )
            except oss2.exceptions.OssError as exc:
                if _is_no_such_upload(exc):
                    return None
                raise map_oss_error("OSS multipart parts listing", exc) from exc
            for part in page.parts:
                part_count += 1
                size_bytes += int(part.size or 0)
            if not page.is_truncated:
                return part_count, size_bytes
            marker = page.next_marker
