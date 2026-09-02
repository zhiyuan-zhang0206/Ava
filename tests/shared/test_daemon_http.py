"""The shared transport stays Settings-free; real sockets preserve auth/routes."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from shared.daemon_http import start_daemon_http


def test_transport_import_cannot_fetch_gateway_configuration() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(  # noqa: S603 — fixed interpreter and repository-owned probe only
        [
            sys.executable,
            "-I",
            "-c",
            """import sys
import importlib.abc
sys.path.insert(0, sys.argv[1])
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.startswith(('shared.config', 'shared.bootstrap', 'plugins',
                                'services.agent_ops.daemon')):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Deny())
import shared.daemon_http
print('TRANSPORT_ONLY')
""",
            str(root),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout.strip() == "TRANSPORT_ONLY"


async def _request(port: int, method: str, path: str, token: str | None) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    auth = f"Authorization: Bearer {token}\r\n" if token else ""
    writer.write(
        f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n{auth}Content-Length: 2\r\n\r\n{{}}".encode()
    )
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    headers, _, body = raw.partition(b"\r\n\r\n")
    return int(headers.split(b" ")[1]), body


@pytest.mark.asyncio
async def test_only_explicit_route_and_authenticated_challenge_can_run() -> None:
    calls = 0

    async def observe(_body: bytes) -> tuple[int, bytes, str]:
        nonlocal calls
        calls += 1
        return 200, b'{"mode":"bootstrap_observation"}', "application/json"

    server = await start_daemon_http(
        host="127.0.0.1",
        port=0,
        health_response=lambda: (503, b'{"mode":"bootstrap_observation","full_ready":false}'),
        extra_routes={("POST", "/ops/bootstrap-observation"): observe},
        auth_token="test-cluster-secret",  # noqa: S106 — isolated socket fixture
    )
    port = server.sockets[0].getsockname()[1]
    try:
        status, body = await _request(port, "GET", "/healthz", None)
        assert status == 503
        assert json.loads(body)["full_ready"] is False
        assert (await _request(port, "POST", "/ops", "test-cluster-secret"))[0] == 404
        assert (await _request(port, "POST", "/ops/bootstrap-observation", None))[0] == 401
        assert (await _request(port, "POST", "/ops/bootstrap-observation", "wrong"))[0] == 401
        assert calls == 0
        assert (await _request(port, "POST", "/ops/bootstrap-observation", "test-cluster-secret"))[
            0
        ] == 200
        assert calls == 1
    finally:
        server.close()
        await server.wait_closed()
