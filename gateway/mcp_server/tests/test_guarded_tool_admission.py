"""An older MCP tool table cannot admit guarded creation through legacy spawn."""

from typing import Any

import psycopg
import pytest

from gateway.app import app
from gateway.mcp_server import endpoint
from gateway.tests.extensions.test_mcp_endpoint import (
    _create_token,
    _tool_call,
)
from gateway.tests.extensions.test_mcp_endpoint import (
    _enable_endpoint as _enable_endpoint,
)
from tests.fixtures.gateway_config import gateway_test_client


def test_older_server_rejects_guarded_tool_before_creating_anything(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp.server.mcpserver import MCPServer

    def older_manager(*args: object, **kwargs: object) -> Any:
        server = MCPServer("older-ava")

        @server.tool()
        async def spawn_agent(prompt: str) -> dict[str, object]:
            raise AssertionError("guarded intent cannot fall back to legacy creation")

        server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, host="")
        return server.session_manager

    monkeypatch.setattr(endpoint, "build_manager", older_manager)
    with gateway_test_client(app) as client:
        token = _create_token(client)
        response = _tool_call(
            client,
            token,
            "spawn_agent_guarded_v1",
            {"prompt": "one original goal", "idempotency_key": "original"},
        )
    assert response.get("error") or response["result"].get("isError")
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (0,)
