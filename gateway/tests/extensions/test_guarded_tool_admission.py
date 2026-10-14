"""An older MCP tool table cannot admit guarded creation through legacy spawn."""

from typing import Any

import psycopg
import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from gateway.app import app
from gateway.mcp_server import endpoint
from gateway.tests.mcp_calls import create_token, tool_call
from gateway.tests.mcp_calls import enable_endpoint as enable_endpoint
from tests.fixtures.gateway_config import gateway_test_client


def test_older_server_rejects_guarded_tool_before_creating_anything(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from mcp.server.mcpserver import MCPServer

    def older_manager(*args: object, local_machine: MachineIdentity, **kwargs: object) -> Any:
        del local_machine
        server = MCPServer("older-ava")

        @server.tool()
        async def spawn_agent(prompt: str) -> dict[str, object]:
            raise AssertionError("guarded intent cannot fall back to legacy creation")

        server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, host="")
        return server.session_manager

    monkeypatch.setattr(endpoint, "build_manager", older_manager)
    with gateway_test_client(app, machine=machine_identity) as client:
        token = create_token(client)
        response = tool_call(
            client,
            token,
            "spawn_agent_guarded_v1",
            {"prompt": "one original goal", "idempotency_key": "original"},
        )
    assert response.get("error") or response["result"].get("isError")
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (0,)
