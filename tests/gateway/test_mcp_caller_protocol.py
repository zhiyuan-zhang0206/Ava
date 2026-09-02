"""Opted-in MCP provenance is token-bound and passes the actual target gate."""

from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.graph._chat_inbound import build_chat_inbound
from gateway.app import app
from shared.config import settings
from tests.gateway.test_caller_protocol_path import _admit, _after_proven_old_writer_barrier
from tests.gateway.test_mcp_endpoint import _ACCEPT, _initialize, _tool_call


@pytest.fixture(autouse=True)
def _enable_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.gateway, "mcp_endpoint_enabled", True)


def _failed(result: dict[str, Any]) -> bool:
    return "error" in result or bool(result["result"].get("isError"))


async def test_token_derived_mcp_source_reaches_real_claim(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    incarnation = await _admit(db_conn, aops_pool)
    _after_proven_old_writer_barrier(db_conn, incarnation)
    with TestClient(app) as client:
        response = client.post("/api/mcp/clients", json={"name": "not-authority", "scope": "write"})
        assert response.status_code == 200
        credential = response.json()
        _initialize(client, credential["token"])
        result = _tool_call(
            client,
            credential["token"],
            "send_message",
            {
                "agent_id": incarnation.agent_id,
                "content": "mcp proof",
                "caller_protocol": "v1",
            },
        )
        assert not _failed(result), result
    claimed = await claim_inbound_batch(aops_pool, incarnation.agent_id)
    assert len(claimed) == 1
    assert claimed[0].source == f"external_agent:mcp:{credential['id']}"
    assert claimed[0].payload == {
        "caller_identity": {
            "kind": "external_agent",
            "subject": "mcp",
            "instance": str(credential["id"]),
        }
    }
    assert "External agent" in str(build_chat_inbound(claimed[0]).content)


@pytest.mark.parametrize("denial", ["revoked", "read", "source", "instance", "protocol0", "stale"])
async def test_mcp_denials_insert_nothing(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, denial: str
) -> None:
    incarnation = await _admit(db_conn, aops_pool)
    if denial != "protocol0":
        _after_proven_old_writer_barrier(db_conn, incarnation)
    if denial == "stale":
        db_conn.execute(
            "UPDATE agents_meta SET lease_expires_at = NULL WHERE id = %s", (incarnation.agent_id,)
        )
        db_conn.commit()
    with TestClient(app) as client:
        credential = client.post(
            "/api/mcp/clients",
            json={
                "name": "mcp-denial",
                "scope": "read" if denial == "read" else "write",
            },
        ).json()
        _initialize(client, credential["token"])
        args: dict[str, Any] = {
            "agent_id": incarnation.agent_id,
            "content": "must not insert",
            "caller_protocol": "v1",
        }
        if denial in {"source", "instance"}:
            args[denial] = "user" if denial == "source" else "spoofed"
        if denial == "revoked":
            assert client.post(f"/api/mcp/clients/{credential['id']}/revoke").status_code == 200
            result = client.post(
                "/mcp",
                headers={
                    "Accept": _ACCEPT,
                    "Authorization": f"Bearer {credential['token']}",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "send_message",
                        "arguments": args,
                    },
                },
            )
            assert result.status_code == 401
        else:
            assert _failed(_tool_call(client, credential["token"], "send_message", args))
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (incarnation.agent_id,)
    ).fetchone() == (0,)
