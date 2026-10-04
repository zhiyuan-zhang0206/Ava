# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

"""An in-memory OSS bucket for the off-site publisher tests.

Not a test module: pytest never collects this file. The fake sits at the
transport boundary `offsite.OssBucket` narrows to and behaves as OSS does for
the verbs a publish uses: per-part Content-MD5 verification, the
forbid-overwrite precondition on CompleteMultipartUpload, the deterministic
multipart ETag chain (uppercase hex, as OSS answers), and user metadata.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import oss2
from oss2.models import PartInfo


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324 -- OSS content digest


def chain_etag(parts: Sequence[bytes]) -> str:
    """The ETag OSS gives a multipart object whose parts are `parts`."""
    token = "".join(md5_hex(part).upper() for part in parts)
    return f"{md5_hex(token.encode()).upper()}-{len(parts)}"


def server_error(status: int, code: str) -> oss2.exceptions.ServerError:
    return oss2.exceptions.ServerError(status, {}, b"", {"Code": code, "Message": code})


@dataclass
class _Result:
    etag: str | None = None
    upload_id: str | None = None
    content_length: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict[str, str])


def _meta(headers: Mapping[str, str] | None) -> dict[str, str]:
    return {k: v for k, v in (headers or {}).items() if k.startswith("x-oss-meta-")}


class FakeOssBucket:
    """Stateful in-memory bucket narrowing `offsite.OssBucket`."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.aborted: list[str] = []
        #: (verb, headers) of every call that carried headers, in order.
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.corrupt_part_etags = False
        self.corrupt_complete_etag = False
        self.fail_init: tuple[int, str] | None = None
        self._next_id = 1

    def seed(
        self, key: str, parts: Sequence[bytes], metadata: Mapping[str, str] | None = None
    ) -> None:
        """An object a previous run completed: `parts` joined, ETag the chain."""
        self.files[key] = {
            "etag": chain_etag(parts),
            "size": sum(len(part) for part in parts),
            "headers": {f"x-oss-meta-{k}": v for k, v in (metadata or {}).items()},
            "data": b"".join(parts),
        }

    def head_object(self, key: str) -> _Result:
        if key not in self.files:
            raise server_error(404, "NoSuchKey")
        record = self.files[key]
        return _Result(
            etag=f'"{record["etag"]}"',  # OSS quotes ETags on the wire
            content_length=record["size"],
            headers=dict(record["headers"]),
        )

    def init_multipart_upload(self, key: str, headers: Mapping[str, str] | None = None) -> _Result:
        self.calls.append(("init", dict(headers or {})))
        if self.fail_init is not None:
            raise server_error(*self.fail_init)
        upload_id = f"up{self._next_id}"
        self._next_id += 1
        self.pending[upload_id] = {"key": key, "headers": _meta(headers), "parts": {}}
        return _Result(upload_id=upload_id)

    def upload_part(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        data: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> _Result:
        self.calls.append(("part", dict(headers or {})))
        expected = (headers or {}).get("Content-MD5")
        if expected != base64.b64encode(bytes.fromhex(md5_hex(data))).decode():
            raise server_error(400, "InvalidDigest")
        self.pending[upload_id]["parts"][part_number] = data
        return _Result(etag="Z" * 32 if self.corrupt_part_etags else md5_hex(data).upper())

    def complete_multipart_upload(
        self,
        key: str,
        upload_id: str,
        parts: Sequence[PartInfo],
        headers: Mapping[str, str] | None = None,
    ) -> _Result:
        self.calls.append(("complete", dict(headers or {})))
        if key in self.files and (headers or {}).get("x-oss-forbid-overwrite") == "true":
            raise server_error(409, "FileAlreadyExists")
        pending = self.pending.pop(upload_id)
        bodies = [pending["parts"][part.part_number] for part in parts]
        etag = chain_etag(bodies)
        if self.corrupt_complete_etag:
            etag = etag[:-3] + "999"
        self.files[key] = {
            "etag": etag,
            "size": sum(len(body) for body in bodies),
            "headers": pending["headers"],
            "data": b"".join(bodies),
        }
        return _Result(etag=etag)

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        self.pending.pop(upload_id)
        self.aborted.append(upload_id)
