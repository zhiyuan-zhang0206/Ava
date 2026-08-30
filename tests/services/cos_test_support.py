"""Shared fixtures for the Tencent Cloud COS adapter tests.

Not a test module: pytest never collects this file. The fake COS
S3-compatible plane lives here so the adapter test files exercise one
honest in-memory backend, including SigV4 verification recomputed from
the wire request and COS's Content-MD5 / If-None-Match semantics.
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

import httpx

from services.pitr.cos_client import CosClient, CosCredentials, _signature_v4

BUCKET = "ava-pitr-test-1250000000"
REGION = "ap-guangzhou"
SECRET_ID = "AKIDtest"  # noqa: S105 — fixture identity
SECRET_KEY = "secret-key-test"  # noqa: S105 — fixture identity


def cos_credentials() -> CosCredentials:
    return CosCredentials(secret_id=SECRET_ID, secret_key=SECRET_KEY, region=REGION, bucket=BUCKET)


def cos_client_for(fake: FakeCos) -> CosClient:
    return CosClient(cos_credentials(), transport=httpx.MockTransport(fake.handler))


_META_PREFIXES = ("x-amz-meta-", "x-cos-meta-")


def _request_metadata(request: httpx.Request) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for header, value in request.headers.items():
        for prefix in _META_PREFIXES:
            if header.startswith(prefix):
                metadata[header[len(prefix) :]] = value
    return metadata


class FakeCos:
    """Stateful in-memory COS S3-compatible plane for one test."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.precondition_race_keys: set[str] = set()
        self.reject_put_keys: set[str] = set()
        self.etag_overrides: dict[str, str] = {}
        self.corrupt_get_keys: set[str] = set()
        self.corrupt_bytes_keys: set[str] = set()
        self.page_size = 1000

    def seed(self, key: str, body: bytes, metadata: dict[str, str] | None = None) -> str:
        digest = hashlib.md5(body).hexdigest()  # noqa: S324 — COS ETag digest
        self.objects[key] = {"body": body, "metadata": dict(metadata or {})}
        return digest

    def _etag(self, key: str) -> str:
        if key in self.etag_overrides:
            return self.etag_overrides[key]
        return hashlib.md5(self.objects[key]["body"]).hexdigest()  # noqa: S324 — COS ETag digest

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        self._verify_auth(request)
        path = request.url.path.lstrip("/")
        if request.method == "GET" and request.url.params.get("list-type") == "2":
            return self._list(request)
        if request.method == "PUT":
            return self._put(request, path)
        if request.method == "HEAD":
            return self._head(path)
        if request.method == "GET":
            return self._get(request, path)
        return httpx.Response(405)

    def _verify_auth(self, request: httpx.Request) -> None:
        """Recompute the SigV4 signature from the wire request fields and
        require it to match what the client sent (plumbing check)."""
        authorization = request.headers.get("authorization", "")
        match = re.search(r"Signature=([0-9a-f]{64})", authorization)
        assert match is not None, "missing SigV4 signature"
        signed_headers = re.search(r"SignedHeaders=([^,]+)", authorization)
        assert signed_headers is not None, "missing SignedHeaders"
        headers = {
            name: request.headers.get(name, "") for name in signed_headers.group(1).split(";")
        }
        expected = _signature_v4(
            method=request.method,
            url_path=request.url.path,
            query=dict(request.url.params),
            headers=headers,
            payload_hash=request.headers.get("x-amz-content-sha256", ""),
            access_key_id=SECRET_ID,
            secret_access_key=SECRET_KEY,
            region=REGION,
            amz_date=request.headers["x-amz-date"],
        )
        assert expected.endswith(match.group(1)), "SigV4 signature mismatch"

    def _put(self, request: httpx.Request, path: str) -> httpx.Response:
        if path in self.reject_put_keys:
            return httpx.Response(403)
        if request.headers.get("if-none-match") == "*":
            if path in self.precondition_race_keys:
                return httpx.Response(412)
            if path in self.objects:
                return httpx.Response(412)
        body = request.read()
        content_md5 = request.headers.get("content-md5", "")
        if content_md5 and hashlib.md5(body).digest() != base64.b64decode(content_md5):  # noqa: S324 — COS MD5 check
            return httpx.Response(400, text="BadDigest")
        metadata = _request_metadata(request)
        digest = hashlib.md5(body).hexdigest()  # noqa: S324 — COS ETag digest
        self.objects[path] = {"body": body, "metadata": metadata}
        return httpx.Response(200, headers={"ETag": f'"{digest}"'})

    def _head(self, path: str) -> httpx.Response:
        row = self.objects.get(path)
        if row is None:
            return httpx.Response(404)
        return self._object_response(row, path)

    def _get(self, request: httpx.Request, path: str) -> httpx.Response:
        row = self.objects.get(path)
        if row is None:
            return httpx.Response(404)
        etag = self._etag(path)
        if_match = request.headers.get("if-match")
        if if_match is not None and if_match != etag:
            return httpx.Response(412)
        body = row["body"]
        if path in self.corrupt_get_keys:
            body = body + b"tampered"
        if path in self.corrupt_bytes_keys and body:
            body = body[:-1] + bytes([body[-1] ^ 0xFF])
        response = self._object_response(row, path)
        return httpx.Response(200, headers=response.headers, content=body)

    def _object_response(self, row: dict[str, Any], path: str) -> httpx.Response:
        headers = {
            "ETag": f'"{self._etag(path)}"',
            "Content-Length": str(len(row["body"])),
        }
        for key, value in row["metadata"].items():
            headers[f"x-amz-meta-{key}"] = value
        return httpx.Response(200, headers=headers)

    def _list(self, request: httpx.Request) -> httpx.Response:
        prefix = str(request.url.params.get("prefix", ""))
        token = request.url.params.get("continuation-token")
        keys = sorted(
            key for key in self.objects if key.startswith(prefix) and not key.endswith("/")
        )
        start = 0 if token is None else int(token)
        page = keys[start : start + self.page_size]
        truncated = start + self.page_size < len(keys)
        next_token = str(start + self.page_size) if truncated else None
        contents = "".join(
            f"<Contents><Key>{key}</Key><ETag>{self._etag(key)}</ETag>"
            f"<Size>{len(self.objects[key]['body'])}</Size></Contents>"
            for key in page
        )
        next_text = (
            f"<NextContinuationToken>{next_token}</NextContinuationToken>" if truncated else ""
        )
        xml = (
            "<ListBucketResult>"
            f"<Name>{BUCKET}</Name><Prefix>{prefix}</Prefix>"
            f"<KeyCount>{len(page)}</KeyCount><IsTruncated>{str(truncated).lower()}</IsTruncated>"
            f"{next_text}{contents}</ListBucketResult>"
        )
        return httpx.Response(200, content=xml.encode())
