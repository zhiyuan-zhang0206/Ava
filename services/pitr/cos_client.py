"""Tencent Cloud COS adapter via its S3-compatible API (AWS SigV4).

Why not cos-python-sdk-v5: the official SDK pins
``requests<=2.27.1`` + ``certifi<=2021.10.8`` (PyPI 1.9.44) — a
dependency floor that conflicts with this stack (requests>=2.32,
httpx>=0.28) and drags in crcmod/pycryptodome for what is a small
REST surface. The COS S3-compatible API instead accepts standard AWS
SigV4 over the existing httpx client, so the adapter hands its own
minimal signer; the signing math is pinned against the documented AWS
SigV4 test vector in tests/services/test_pitr_cos_signing.py.

Backend identity vocabulary for one object:

- ``pin_token`` = the object ETag (hex MD5 for a simple PUT — the only
  upload verb this backend publishes with; COS caps a simple PUT at
  5 GiB and the adapter enforces that ceiling instead of falling back
  to a multipart ETag, which is a composite of the part digests and no
  longer equals the object content MD5).
- ``checksum`` = (md5, ETag) — COS verifies ``Content-MD5`` on the
  simple PUT and sets ETag to that same digest, so the ETag IS the
  backend-verified content digest.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import stat
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
"""Payload hash for GET/HEAD — the AWS SigV4 empty-body digest."""

_META_PREFIXES = ("x-amz-meta-", "x-cos-meta-")
"""COS stores custom headers as x-cos-meta-* natively and converts to
x-amz-meta-* on the S3-compatible interface; both spellings are read."""

_META_KEY = re.compile(r"^[a-z0-9][a-z0-9-]*$")
"""Metadata keys must be lowercase ASCII (x-amz-meta-{key} header rule)."""


class CosClientError(RuntimeError):
    """A COS S3-compatible request failed."""


class CosNotFoundError(CosClientError):
    """The object does not exist."""


class CosPreconditionFailedError(CosClientError):
    """A conditional write/read was refused (412) — object exists/changed."""


class CosTransientError(CosClientError):
    """The request may be retried by the outer uploader loop."""


class CosPermanentError(CosClientError):
    """The request cannot succeed without operator action."""


@dataclass(frozen=True)
class CosObjectRow:
    """The identity COS publishes for one object (HEAD or PUT response)."""

    key: str
    etag: str
    size: int
    metadata: dict[str, str]


def credential_evidence(credentials_file: Path, *, region: str, bucket: str) -> dict[str, str]:
    """CLI activation evidence for the COS backend.

    Returns the SecretId identity and the bucket target. The 0600/file
    safety is enforced by the settings validator at load time; the read
    scope is proven at activation time by the viewer read-back, so no
    live probe is issued here.
    """
    try:
        info = credentials_file.lstat()
    except OSError as exc:
        raise CosPermanentError("COS credentials file is missing") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or credentials_file.is_symlink()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise CosPermanentError("COS credentials file is unsafe")
    credentials = CosCredentials.from_file(credentials_file, region=region, bucket=bucket)
    return {
        "backend": "cos",
        "uploader_identity": credentials.secret_id,
        "viewer_identity": credentials.secret_id,
        "store_target": credentials.bucket,
    }


@dataclass(frozen=True)
class CosCredentials:
    """Static SecretId/SecretKey pair + the bucket/region identity."""

    secret_id: str
    secret_key: str
    region: str
    bucket: str

    @property
    def endpoint_host(self) -> str:
        return f"{self.bucket}.cos.{self.region}.myqcloud.com"

    @classmethod
    def from_file(cls, path: Path, *, region: str, bucket: str) -> CosCredentials:
        """Strict parse of the 0600 credentials JSON; any deviation raises.

        The shape is ``{"secret_id": "...", "secret_key": "..."}`` —
        the S3-compatible interface maps these onto AccessKey/SecretKey.
        """
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise CosPermanentError("COS credentials file is unreadable") from exc
        if not isinstance(raw, dict):
            raise CosPermanentError("COS credentials payload must be an object")
        payload = cast(dict[str, Any], raw)
        secret_id = payload.get("secret_id")
        secret_key = payload.get("secret_key")
        if not isinstance(secret_id, str) or not secret_id:
            raise CosPermanentError("COS credentials lack a secret_id")
        if not isinstance(secret_key, str) or not secret_key:
            raise CosPermanentError("COS credentials lack a secret_key")
        return cls(secret_id=secret_id, secret_key=secret_key, region=region, bucket=bucket)


def _hmac_sha256(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode(), hashlib.sha256).digest()


def _signature_v4(
    *,
    method: str,
    url_path: str,
    query: Mapping[str, str] | None,
    headers: Mapping[str, str],
    payload_hash: str,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    amz_date: str,
) -> str:
    """AWS SigV4 Authorization signature for one S3-shaped request.

    The canonical request follows the S3 rules: the URI path is not
    normalized (slashes stay, each segment percent-encoded), the query
    is sorted by encoded key, and exactly the headers supplied are
    signed (host + x-amz-date + x-amz-content-sha256 plus any
    x-amz-meta-*/Content headers the verb carries).
    """
    canon_uri = "/" + "/".join(
        urllib.parse.quote(segment, safe="-_.~") for segment in url_path.lstrip("/").split("/")
    )
    canon_query = "&".join(
        f"{urllib.parse.quote(key, safe='-_.~')}={urllib.parse.quote(value, safe='-_.~')}"
        for key, value in sorted((query or {}).items())
    )
    normalized = {str(key).lower(): str(value).strip() for key, value in headers.items()}
    canon_headers = "".join(f"{key}:{normalized[key]}\n" for key in sorted(normalized))
    signed_headers = ";".join(sorted(normalized))
    canonical_request = (
        f"{method.upper()}\n{canon_uri}\n{canon_query}\n{canon_headers}\n"
        f"{signed_headers}\n{payload_hash}"
    )
    scope = f"{amz_date[:8]}/{region}/s3/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )
    date_key = _hmac_sha256(("AWS4" + secret_access_key).encode(), amz_date[:8])
    region_key = _hmac_sha256(date_key, region)
    service_key = _hmac_sha256(region_key, "s3")
    signing_key = _hmac_sha256(service_key, "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def canonical_metadata(value: Mapping[str, str]) -> dict[str, str]:
    """Validate metadata and convert it to the x-amz-meta-* header
    vocabulary (COS S3-compat stores these and echoes them back)."""
    for key in value:
        if _META_KEY.fullmatch(key) is None:
            raise CosPermanentError(f"COS metadata key is not header-safe: {key!r}")
    return {f"x-amz-meta-{key}": str(meta_value) for key, meta_value in value.items()}


def response_metadata(headers: Mapping[str, str]) -> dict[str, str]:
    """Read x-amz-meta-* / x-cos-meta-* headers out of a response."""
    metadata: dict[str, str] = {}
    for header, value in headers.items():
        for prefix in _META_PREFIXES:
            if header.startswith(prefix):
                metadata[header[len(prefix) :]] = value
    return metadata


def _content_md5_header(digest_hex: str) -> str:
    """Content-MD5 must carry the Base64-encoded MD5 digest (COS docs
    436/32467, 436/36427) — a 32-char hex string decodes to the wrong
    byte length and the real API rejects it with 400 BadDigest."""
    return base64.b64encode(bytes.fromhex(digest_hex)).decode("ascii")


def _unquote_etag(value: str | None) -> str:
    if value is None:
        raise CosPermanentError("COS response omitted the object ETag")
    return value.strip().strip('"')


class CosClient:
    """Thin signed S3-compatible COS client; no retry layer (the outer
    uploader loop retries transient errors).

    Transport injection keeps tests on an in-memory plane (the shared
    ``httpx.MockTransport`` pattern the Baidu adapter uses for PCS).
    """

    def __init__(
        self,
        credentials: CosCredentials,
        *,
        timeout_seconds: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self._timeout_seconds = timeout_seconds
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
        )
        self._owns_client = transport is None

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        key: str,
        *,
        query: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | Iterator[bytes] | None = None,
        content_length: int | None = None,
    ) -> httpx.Response:
        url_path = f"/{key}"
        host = self._credentials.endpoint_host
        amz_date = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        if isinstance(content, bytes):
            payload_hash = hashlib.sha256(content).hexdigest()
        elif content is None:
            payload_hash = _EMPTY_SHA256
        else:
            stream_hash = (headers or {}).get("x-amz-content-sha256")
            if stream_hash is None:
                raise CosPermanentError("streamed COS request lacks x-amz-content-sha256")
            payload_hash = str(stream_hash)
        signed: dict[str, str] = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        signed.update(dict(headers or {}))
        authorization = _signature_v4(
            method=method,
            url_path=url_path,
            query=query,
            headers=signed,
            payload_hash=payload_hash,
            access_key_id=self._credentials.secret_id,
            secret_access_key=self._credentials.secret_key,
            region=self._credentials.region,
            amz_date=amz_date,
        )
        request_headers = dict(signed)
        request_headers["Authorization"] = authorization
        if content_length is not None:
            request_headers["Content-Length"] = str(content_length)
        url = f"https://{host}{url_path}"
        try:
            return self._client.request(
                method.upper(),
                url,
                params=query,
                headers=request_headers,
                content=content,
            )
        except httpx.HTTPError as exc:
            raise CosTransientError(f"COS {method.lower()} transport failed") from exc

    def _raise_for_status(self, response: httpx.Response, *, operation: str) -> None:
        if response.status_code == 200:
            return
        if response.status_code == 404:
            raise CosNotFoundError(f"COS {operation}: object not found")
        if response.status_code in (409, 412):
            raise CosPreconditionFailedError(f"COS {operation}: precondition failed")
        if response.status_code >= 500:
            raise CosTransientError(f"COS {operation}: HTTP {response.status_code}")
        raise CosPermanentError(f"COS {operation}: HTTP {response.status_code}")

    @staticmethod
    def _row(
        key: str, response: httpx.Response, *, size_override: int | None = None
    ) -> CosObjectRow:
        etag = _unquote_etag(response.headers.get("etag"))
        if size_override is not None:
            size = size_override
        else:
            raw_size = response.headers.get("content-length")
            if raw_size is None:
                raise CosPermanentError("COS response omitted the object size")
            size = int(raw_size)
        return CosObjectRow(
            key=key,
            etag=etag,
            size=size,
            metadata=response_metadata(response.headers),
        )

    def head_object(self, key: str) -> CosObjectRow | None:
        response = self._request("HEAD", key)
        try:
            self._raise_for_status(response, operation="head object")
        except CosNotFoundError:
            return None
        finally:
            response.close()
        return self._row(key, response)

    def put_object_bytes(
        self,
        key: str,
        *,
        body: bytes,
        metadata: Mapping[str, str],
        if_none_match: bool = True,
    ) -> CosObjectRow:
        headers = {
            "Content-MD5": _content_md5_header(hashlib.md5(body).hexdigest())  # noqa: S324 — COS content digest
        }
        headers.update(canonical_metadata(metadata))
        if if_none_match:
            headers["If-None-Match"] = "*"
        response = self._request(
            "PUT", key, headers=headers, content=body, content_length=len(body)
        )
        try:
            self._raise_for_status(response, operation="put object")
        finally:
            response.close()
        return self._row(key, response, size_override=len(body))

    def put_object_stream(
        self,
        key: str,
        *,
        body: Iterator[bytes],
        content_length: int,
        content_md5: str,
        payload_sha256: str,
        metadata: Mapping[str, str],
        if_none_match: bool = True,
    ) -> CosObjectRow:
        headers = {
            "Content-MD5": _content_md5_header(content_md5),
            "x-amz-content-sha256": payload_sha256,
        }
        headers.update(canonical_metadata(metadata))
        if if_none_match:
            headers["If-None-Match"] = "*"
        response = self._request(
            "PUT",
            key,
            headers=headers,
            content=body,
            content_length=content_length,
        )
        try:
            self._raise_for_status(response, operation="put object stream")
        finally:
            response.close()
        return self._row(key, response, size_override=content_length)

    def get_object(self, key: str, *, if_match: str | None = None) -> httpx.Response:
        """Open one object for streaming read; the caller closes it."""
        headers: dict[str, str] = {}
        if if_match is not None:
            headers["If-Match"] = if_match
        response = self._request("GET", key, headers=headers)
        self._raise_for_status(response, operation="get object")
        return response

    def get_object_bytes(self, key: str) -> bytes | None:
        try:
            response = self.get_object(key)
        except CosNotFoundError:
            return None
        try:
            return response.content
        finally:
            response.close()

    def list_object_keys(self, prefix: str) -> Iterator[str]:
        """List every key under ``prefix`` (ListObjectsV2, paged)."""
        token: str | None = None
        while True:
            query: dict[str, str] = {"list-type": "2", "prefix": prefix}
            if token is not None:
                query["continuation-token"] = token
            response = self._request("GET", "", query=query)
            try:
                self._raise_for_status(response, operation="list objects")
                keys, token, truncated = _parse_list_v2(response.content)
            finally:
                response.close()
            yield from keys
            if not truncated:
                return


def _parse_list_v2(data: bytes) -> tuple[list[str], str | None, bool]:
    """Parse a ListBucketResult; XML namespace prefixes are ignored."""
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise CosPermanentError("COS listing carries a disallowed DTD")
    try:
        root = ET.fromstring(data)  # noqa: S314 — endpoint-controlled, DTD rejected above
    except ET.ParseError as exc:
        raise CosPermanentError("COS listing is not valid XML") from exc
    keys: list[str] = []
    token: str | None = None
    truncated = False
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "Key" and element.text:
            keys.append(element.text)
        elif tag == "NextContinuationToken" and element.text:
            token = element.text
        elif tag == "IsTruncated" and element.text:
            truncated = element.text.strip().lower() == "true"
    return keys, token, truncated
