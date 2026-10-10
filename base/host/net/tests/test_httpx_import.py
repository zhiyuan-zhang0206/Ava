"""HTTPX public APIs remain usable after a fresh host-network import."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _probe(code: str) -> dict[str, object]:
    """Run `code` in a fresh interpreter and return the JSON object it prints last.

    `code` is a literal that puts its `sys.argv[1]` (the repo root) on `sys.path`, so
    test selection can read the probe's imports.
    """
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-I", "-X", "utf8", "-c", code, str(_REPO_ROOT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_httpx_sync_and_async_transports_after_host_network_import() -> None:
    report = _probe(
        """import json
import sys

sys.path.insert(0, sys.argv[1])

import asyncio

import base.host.net
import httpx


def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"path": request.url.path})


with httpx.Client(transport=httpx.MockTransport(handler)) as client:
    assert client.get("http://x.test/sync").json() == {"path": "/sync"}


async def async_get() -> object:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return (await client.get("http://x.test/async")).json()


assert asyncio.run(async_get()) == {"path": "/async"}
print(json.dumps({"transport_ok": True}))
"""
    )
    assert report["transport_ok"] is True


def test_host_network_import_preserves_httpx_cli_help() -> None:
    report = _probe(
        """import json
import sys

sys.path.insert(0, sys.argv[1])

import base.host.net
import httpx

sys.argv = ["httpx", "--help"]
try:
    httpx.main()
except SystemExit as error:
    print(json.dumps({"exit_code": error.code}))
"""
    )
    assert report["exit_code"] == 0
