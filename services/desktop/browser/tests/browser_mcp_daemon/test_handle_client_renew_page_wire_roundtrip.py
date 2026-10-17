from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from services.desktop.browser.mcp_daemon import ChromeMcpDaemon, handle_client
from services.desktop.browser.tests.test_browser_mcp_daemon import FakeUpstream, new_daemon


async def test_handle_client_renew_page_wire_roundtrip() -> None:
    import contextlib

    up = FakeUpstream()
    daemon_ref: list[ChromeMcpDaemon | None] = [new_daemon(up)]
    sock_path = Path(tempfile.gettempdir()) / f"meiva-bmd-{uuid4().hex}.sock"
    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, daemon_ref), path=sock_path
    )

    async def roundtrip(payload: dict[str, Any]) -> dict[str, Any]:
        reader, writer = await asyncio.open_unix_connection(path=sock_path)
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return json.loads(line)

    try:
        opened = await roundtrip(
            {
                "id": 1,
                "method": "call_tool",
                "tool": "new_page",
                "args": {"url": "x"},
                "agent_id": 7,
            }
        )
        assert opened["ok"] is True
        renewed = await roundtrip(
            {
                "id": 1,
                "method": "call_tool",
                "tool": "renew_page",
                "args": {"ttl": 120},
                "agent_id": 7,
            }
        )
        assert renewed["ok"] is True
        assert renewed["result"]["is_error"] is False
        assert "renewed" in renewed["result"]["content"][0]["text"]
    finally:
        server.close()
        await server.wait_closed()
        sock_path.unlink(missing_ok=True)
