"""Shared fixtures for the Baidu Netdisk adapter tests.

Not a test module: pytest never collects this file. The fake PCS control
plane and the protocol-compatible token manager live here so the two
adapter test files exercise one honest in-memory backend.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.pitr.baidu_pcs import PcsClient
from services.pitr.baidu_restore_store import BaiduGenerationPinnedObjectReader
from services.pitr.baidu_store import BaiduObjectStore
from services.pitr.object_store import RemoteObjectAck
from services.pitr.token_manager import TokenHealth

APP_ROOT = "/apps/ava-pitr"
OBJECT = "ava-pitr/wal/00000001/000000010000000000000001.enc"


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324 — fixture digest


class FakeTokenManager:
    """Protocol-compatible token supplier; no OAuth exchange."""

    def __init__(self, token: str = "access-token") -> None:  # noqa: S107 — fixture token
        self._token = token

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        return self._token

    def health(self) -> TokenHealth:
        return TokenHealth(
            remaining_seconds=None, expires_at=None, last_refresh_at=None, refresh_error=None
        )


class FakePcs:
    """Stateful in-memory PCS control plane for one test.

    Parts are kept keyed by path (not uploadid): a re-precreate sees the
    shards a previous attempt already uploaded, which is the resume
    semantic the store engine relies on."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.parts: dict[str, dict[int, bytes]] = {}
        self.calls: list[str] = []
        self.rapid_paths: set[str] = set()
        self.collision_paths: set[str] = set()
        self.transient_paths: set[str] = set()
        self.missing_override: dict[str, list[str]] = {}
        self._next_fs_id = 100
        self._next_uploadid = 0

    def seed_file(
        self, path: str, *, size: int, md5: str, dlink: str | None = None
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "fs_id": self._next_fs_id,
            "path": path,
            "size": size,
            "md5": md5,
            "isdir": 0,
        }
        self._next_fs_id += 1
        if dlink is not None:
            row["dlink"] = dlink
        self.files[path] = row
        return row

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        method = params.get("method")
        path = str(params.get("path") or "")
        self.calls.append(f"{method} {path}")
        if method == "precreate":
            return self._precreate(params, path)
        if method == "upload":
            return self._upload(params, request.content)
        if method == "create":
            return self._create(params, path)
        if method == "filemetas":
            return self._filemetas(params)
        if method == "list":
            return self._list(params)
        if method == "filemanager":
            return httpx.Response(200, json={"errno": 0})
        return httpx.Response(404, json={"errno": 1, "errmsg": "unknown method"})

    def _precreate(self, params: dict[str, str], path: str) -> httpx.Response:
        if path in self.transient_paths:
            return httpx.Response(200, json={"errno": 31198, "errmsg": "too fast"})
        if path in self.collision_paths:
            return httpx.Response(200, json={"errno": 2, "errmsg": "exists"})
        if path in self.rapid_paths:
            return httpx.Response(
                200, json={"uploadid": "u-rapid", "return_type": 2, "block_list": []}
            )
        uploadid = params.get("uploadid") or f"u{self._next_uploadid}"
        if params.get("uploadid") is None:
            self._next_uploadid += 1
        block_list = json.loads(params["block_list"])
        held = self.parts.setdefault(path, {})
        if path in self.missing_override:
            missing = self.missing_override[path]
        else:
            missing = [digest for index, digest in enumerate(block_list) if index not in held]
        return httpx.Response(
            200, json={"uploadid": uploadid, "return_type": 1, "block_list": missing}
        )

    def _upload(self, params: dict[str, str], content: bytes) -> httpx.Response:
        held = self.parts.setdefault(str(params["path"]), {})
        held[int(params["partseq"])] = _file_payload(content)
        return httpx.Response(200, json={"errno": 0})

    def _create(self, params: dict[str, str], path: str) -> httpx.Response:
        if path in self.collision_paths:
            return httpx.Response(200, json={"errno": 2, "errmsg": "exists"})
        block_count = len(json.loads(params["block_list"]))
        content = b"".join(self.parts.get(path, {}).get(i, b"") for i in range(block_count))
        row = self.seed_file(path, size=len(content), md5=_md5(content))
        return httpx.Response(200, json=row)

    def _filemetas(self, params: dict[str, str]) -> httpx.Response:
        fsids = json.loads(params["fsids"])
        want_dlink = params.get("dlink") == "1"
        rows: list[dict[str, Any]] = []
        for row in self.files.values():
            if row["fs_id"] in fsids:
                copy = dict(row)
                if not want_dlink:
                    copy.pop("dlink", None)
                rows.append(copy)
        return httpx.Response(200, json={"list": rows})

    def _list(self, params: dict[str, str]) -> httpx.Response:
        directory = str(params["dir"]).rstrip("/")
        rows = [
            row
            for row in self.files.values()
            if row["path"] == directory or row["path"].startswith(f"{directory}/")
        ]
        return httpx.Response(200, json={"list": rows})


def _file_payload(content: bytes) -> bytes:
    """Extract the single file part payload from a multipart POST body."""
    first_line, _sep, _rest = content.partition(b"\r\n")
    boundary = first_line[2:]
    tail = b"\r\n--" + boundary + b"--\r\n"
    if not content.endswith(tail):
        raise AssertionError("unexpected multipart body shape")
    header_end = content.index(b"\r\n\r\n") + 4
    return content[header_end : -len(tail)]


def pcs_client_for(fake: FakePcs) -> PcsClient:
    return PcsClient("access-token", transport=httpx.MockTransport(fake.handler))


def make_store(fake: FakePcs, monkeypatch: pytest.MonkeyPatch) -> BaiduObjectStore:
    store = BaiduObjectStore(app_root=APP_ROOT, token_manager=FakeTokenManager())
    monkeypatch.setattr(
        store,
        "_client",
        lambda: PcsClient("access-token", transport=httpx.MockTransport(fake.handler)),
    )
    return store


def make_reader(
    fake: FakePcs, monkeypatch: pytest.MonkeyPatch
) -> BaiduGenerationPinnedObjectReader:
    reader = BaiduGenerationPinnedObjectReader(app_root=APP_ROOT, token_manager=FakeTokenManager())
    monkeypatch.setattr(
        reader._store,
        "_client",
        lambda: PcsClient("access-token", transport=httpx.MockTransport(fake.handler)),
    )
    return reader


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


def sidecar_json(object_name: str, ack: RemoteObjectAck) -> bytes:
    payload = {
        "object_name": object_name,
        "pin_token": ack.pin_token,
        "size": ack.size,
        "checksum_algo": ack.checksum.algo,
        "checksum_value": ack.checksum.value,
        "metadata": dict(ack.metadata),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


class FakeTokenResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def credentials_file(tmp_path: Path) -> Path:
    path = tmp_path / "creds.json"
    path.write_text(
        json.dumps({"app_key": "app", "secret_key": "secret", "refresh_token": "refresh-1"})
    )
    return path


def seed_restore_object(
    fake: FakePcs, *, payload: bytes, metadata: dict[str, str]
) -> tuple[dict[str, Any], bytes]:
    digest = _md5(payload)
    obj_path = f"{APP_ROOT}/{OBJECT}"
    row = fake.seed_file(obj_path, size=len(payload), md5=digest, dlink="https://dl.test/obj")
    sidecar = {
        "object_name": OBJECT,
        "pin_token": f"{row['fs_id']}:{digest}",
        "size": len(payload),
        "checksum_algo": "md5",
        "checksum_value": digest,
        "metadata": metadata,
    }
    sidecar_data = json.dumps(sidecar, sort_keys=True, separators=(",", ":")).encode()
    fake.seed_file(
        f"{obj_path}.ack.json",
        size=len(sidecar_data),
        md5=_md5(sidecar_data),
        dlink="https://dl.test/side",
    )
    return row, sidecar_data
