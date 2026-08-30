"""Thin typed client for the Baidu Netdisk PCS (xpan) HTTP API.

Every endpoint shape lives here — callers never build PCS URLs or parse
PCS payloads themselves, so a doc correction lands in one file. The
adapter layers map PCS errors onto the store error taxonomy
(transient / permanent); this module only classifies raw errno values
and HTTP failures.

Upload flow (three phases, official docs):
  precreate  -> upload (per missing shard) -> create
``rtype`` names the collision policy (docs disagree across revisions —
the live P0 smoke pins the semantics; see ``_PrecreatePolicy``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

import httpx

PCS_HOST = "https://pan.baidu.com"
"""xpan control-plane host (precreate / create / meta / list / manager)."""

UPLOAD_HOST = "https://d.pcs.baidu.com"
"""Upload data-plane host (superfile2) — the shard bytes go here, not to
the control plane (live smoke: pan.baidu.com answers superfile2 with a
WAF page)."""

OAUTH_HOST = "https://openapi.baidu.com"
"""OAuth token host."""

TMPFILE_TYPE = "tmpfile"
"""The only upload ``type`` value the platform accepts today."""

SVIP_SHARD_BYTES = 32 * 1024 * 1024
"""SVIP shard size — the CTO-pinned spec for this deployment (32 MiB)."""

SVIP_SINGLE_FILE_LIMIT_BYTES = 20 * 1024**3
"""SVIP single-file ceiling (20 GB). Base backups must assert below this."""

DOWNLOAD_UA = "pan.baidu.com"
"""The only User-Agent the download data plane accepts."""


class PcsError(RuntimeError):
    """The PCS API rejected a request."""

    def __init__(self, message: str, *, errno: int | None = None) -> None:
        super().__init__(message)
        self.errno = errno


class PcsTransientError(PcsError):
    """Retryable: rate limits, session races, transport failures."""


class PcsPermanentError(PcsError):
    """Operator action needed: auth, quota, size, path, collisions."""


# errno sets are from the official docs; the live P0 smoke verifies them.
_TRANSIENT_ERRNOS = {
    31198,  # called too frequently
    20012,  # unreviewed-app hourly quota — back off, never a terminal error
    31061,  # uploadid missing/expired — re-precreate restarts the session
    31202,  # upload file missing (session lost)
    31203,  # upload block missing (session lost)
    31204,  # upload block sequence error (session lost)
    31205,  # upload block count mismatch (session lost)
    -9,  # file create failed
}
_PERMANENT_ERRNOS = {
    31045,  # token invalid / user not authorized
    31064,  # access denied
    31066,  # user blocked / risk control
    -10,  # file over limit
    -15,  # file size over limit
    31068,  # file size over limit (create)
    31025,  # file size over limit (precreate)
    31021,  # incomplete file name
    31023,  # invalid path
    422,  # file size over limit
    -8,  # file already exists
    2,  # exists with different md5 (rtype=3 precreate)
    3,  # exists (rtype=1 precreate)
    12,  # wrong partseq — a caller bug, never a retry
}


def _check_errno(payload: dict[str, Any]) -> None:
    errno = payload.get("errno")
    if errno in (None, 0):
        return
    errno = cast(int, errno)
    message = str(payload.get("errmsg") or payload.get("error_msg") or "PCS error")
    if errno in _TRANSIENT_ERRNOS:
        raise PcsTransientError(f"PCS errno {errno}: {message}", errno=errno)
    if errno in _PERMANENT_ERRNOS:
        raise PcsPermanentError(f"PCS errno {errno}: {message}", errno=errno)
    raise PcsError(f"PCS errno {errno}: {message}", errno=errno)


@dataclass(frozen=True)
class RemoteFile:
    """One PCS file row (filemetas / list)."""

    fs_id: int
    path: str
    size: int
    md5: str
    isdir: int
    dlink: str | None = None


def _parse_file(raw: dict[str, Any]) -> RemoteFile:
    return RemoteFile(
        fs_id=int(raw["fs_id"]),
        path=str(raw["path"]),
        size=int(raw["size"]),
        md5=str(raw.get("md5") or ""),
        isdir=int(raw["isdir"]),
        dlink=None if raw.get("dlink") is None else str(raw["dlink"]),
    )


@dataclass(frozen=True)
class PrecreateResult:
    uploadid: str
    """Upload session id; the create phase must present it."""

    return_type: int
    """1 = file absent, upload needed; 2 = same-content file exists (rapid
    transfer); 3 = a same-name file exists with different content."""

    missing_blocks: tuple[str, ...]
    """MD5 list of the shards the server still wants (subset of the
    request's block_list, keyed by position in that list)."""


class PcsClient:
    """One app-scoped PCS session; the token supplier is injected."""

    def __init__(
        self,
        token: str,
        *,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = token
        self._timeout = timeout
        self._transport = transport

    def _params(self) -> dict[str, str]:
        return {"access_token": self._token}

    # ── upload: precreate / upload part / create ──

    def precreate(
        self,
        *,
        path: str,
        size: int,
        block_list: list[str],
        rtype: int,
        uploadid: str | None = None,
    ) -> PrecreateResult:
        """Phase 1. ``rtype=3`` gives content-addressed iff-absent:
        same content -> return_type 2 (no upload); different content ->
        errno 2. ``uploadid`` resumption reuses an existing session."""
        params = self._params() | {
            "method": "precreate",
            "path": path,
            "size": str(size),
            "isdir": "0",
            "rtype": str(rtype),
            "autoinit": "1",
            "block_list": json.dumps(block_list, separators=(",", ":")),
        }
        if uploadid is not None:
            params["uploadid"] = uploadid
        payload = self._post(f"{PCS_HOST}/rest/2.0/xpan/file", params=params)
        _check_errno(payload)
        return PrecreateResult(
            uploadid=str(payload["uploadid"]),
            return_type=int(payload["return_type"]),
            missing_blocks=tuple(
                str(item) for item in cast(list[object], payload.get("block_list")) or []
            ),
        )

    def upload_part(self, *, path: str, uploadid: str, partseq: int, data: bytes) -> None:
        """Phase 2. One shard per call, ``partseq`` from 0 ascending."""
        params = self._params() | {
            "method": "upload",
            "type": TMPFILE_TYPE,
            "path": path,
            "uploadid": uploadid,
            "partseq": str(partseq),
        }
        payload = self._post(
            f"{UPLOAD_HOST}/rest/2.0/pcs/superfile2",
            params=params,
            files={"file": ("part", data, "application/octet-stream")},
            headers={"User-Agent": DOWNLOAD_UA},
        )
        _check_errno(payload)

    def create(
        self, *, path: str, size: int, block_list: list[str], uploadid: str, rtype: int
    ) -> RemoteFile:
        """Phase 3. Materializes the file; the response is the read-back row."""
        params = self._params() | {
            "method": "create",
            "path": path,
            "size": str(size),
            "isdir": "0",
            "rtype": str(rtype),
            "uploadid": uploadid,
            "block_list": json.dumps(block_list, separators=(",", ":")),
        }
        payload = self._post(f"{PCS_HOST}/rest/2.0/xpan/file", params=params)
        _check_errno(payload)
        return _parse_file(payload)

    # ── read / enumerate / manage ──

    def filemetas(self, fs_id: int, *, dlink: bool = False) -> RemoteFile | None:
        """One file row by fs_id; None when the file no longer exists."""
        params = self._params() | {
            "method": "filemetas",
            "fsids": json.dumps([fs_id], separators=(",", ":")),
        }
        if dlink:
            params["dlink"] = "1"
        payload = self._get(f"{PCS_HOST}/rest/2.0/xpan/multimedia", params=params)
        _check_errno(payload)
        rows = cast(list[dict[str, Any]], payload.get("list") or [])
        if not rows:
            return None
        return _parse_file(rows[0])

    def list_dir(
        self, dir_path: str, *, start: int = 0, limit: int = 1000, recursion: int = 0
    ) -> list[RemoteFile]:
        """One page of a directory listing; callers page with ``start`` and
        may ask for a recursive walk (``recursion=1``)."""
        params = self._params() | {
            "method": "list",
            "dir": dir_path,
            "order": "name",
            "limit": str(limit),
            "start": str(start),
            "recursion": str(recursion),
        }
        payload = self._get(f"{PCS_HOST}/rest/2.0/xpan/file", params=params)
        _check_errno(payload)
        rows = cast(list[dict[str, Any]], payload.get("list") or [])
        return [_parse_file(row) for row in rows]

    def delete_files(self, paths: list[str]) -> None:
        """Async delete (filemanager opera=delete); fire-and-forget tasks."""
        if not paths:
            return
        params = self._params() | {
            "method": "filemanager",
            "opera": "delete",
            "async": "2",
        }
        payload = self._post(
            f"{PCS_HOST}/rest/2.0/xpan/file",
            params=params,
            data={"filelist": json.dumps(paths, separators=(",", ":"))},
        )
        _check_errno(payload)

    # ── transport ──

    def _get(self, url: str, *, params: dict[str, str]) -> dict[str, Any]:
        try:
            with httpx.Client(transport=self._transport) as client:
                response = client.get(url, params=params, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise PcsTransientError(f"PCS GET {url} transport failure") from exc
        return self._payload(response, url)

    def _post(
        self,
        url: str,
        *,
        params: dict[str, str],
        files: dict[str, tuple[str, bytes, str]] | None = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            with httpx.Client(transport=self._transport) as client:
                response = client.post(
                    url,
                    params=params,
                    files=files,
                    data=data,
                    timeout=self._timeout,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise PcsTransientError(f"PCS POST {url} transport failure") from exc
        return self._payload(response, url)

    @staticmethod
    def _payload(response: httpx.Response, url: str) -> dict[str, Any]:
        if response.status_code >= 500:
            raise PcsTransientError(f"PCS {url} HTTP {response.status_code}")
        if response.status_code in (401, 403):
            raise PcsPermanentError(f"PCS {url} HTTP {response.status_code} (auth rejected)")
        try:
            raw: object = response.json()
        except ValueError as exc:
            raise PcsTransientError(f"PCS {url} returned a non-JSON payload") from exc
        if not isinstance(raw, dict):
            raise PcsTransientError(f"PCS {url} returned a non-object payload")
        return cast(dict[str, Any], raw)
