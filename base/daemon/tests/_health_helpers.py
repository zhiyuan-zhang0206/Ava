"""Shared helpers for the `base.daemon.health` test files; split from base/daemon/tests/test_health.py (task #4922)."""

from __future__ import annotations

import asyncio
import socket


def _find_free_port() -> int:
    """Grabs a free localhost port — OS-assigned to avoid colliding with prod default ports."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _http_get(port: int, path: str) -> tuple[int, bytes]:
    """Simple HTTP 1.1 GET — avoids pulling in httpx/aiohttp test dependency."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    status = int(status_line.split(" ")[1])
    return status, body


def _probe_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/healthz"
