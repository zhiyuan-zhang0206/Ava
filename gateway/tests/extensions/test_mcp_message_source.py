"""MCP `send_message` attribution: the user by default, the authenticated client on request."""

from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.config import settings
from gateway.app import app
from gateway.tests.extensions.test_mcp_endpoint import _initialize, _tool_call, _tool_result


@pytest.fixture(autouse=True)
def _enable_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.gateway, "mcp_endpoint_enabled", True)


def _failed(result: dict[str, Any]) -> bool:
    return "error" in result or bool(result["result"].get("isError"))


def _client_and_agent(client: TestClient) -> tuple[dict[str, Any], int]:
    credential = client.post("/api/mcp/clients", json={"name": "peer", "scope": "write"}).json()
    _initialize(client, credential["token"])
    spawned = _tool_result(
        _tool_call(
            client,
            credential["token"],
            "spawn_agent_guarded_v1",
            {"idempotency_key": "birth-intent", "prompt": "goal"},
        )
    )
    return credential, int(spawned["id"])


def _stored(db: psycopg.Connection, agent_id: int, content: str) -> list[tuple[Any, ...]]:
    return db.execute(
        "SELECT source, payload FROM inbound_messages WHERE agent_id = %s AND content = %s",
        (agent_id, content),
    ).fetchall()


def test_send_message_defaults_to_the_user(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        credential, agent_id = _client_and_agent(client)
        args = {"agent_id": agent_id, "content": "as the user"}
        assert not _failed(_tool_call(client, credential["token"], "send_message", args))
    assert _stored(db_conn, agent_id, "as the user") == [("user", None)]


def test_send_message_as_external_agent_uses_the_client_identity(
    db_conn: psycopg.Connection,
) -> None:
    with TestClient(app) as client:
        credential, agent_id = _client_and_agent(client)
        args = {"agent_id": agent_id, "content": "as a peer", "as_external_agent": True}
        assert not _failed(_tool_call(client, credential["token"], "send_message", args))
    assert _stored(db_conn, agent_id, "as a peer") == [
        (
            f"external_agent:mcp:{credential['id']}",
            {
                "caller_identity": {
                    "kind": "external_agent",
                    "subject": "mcp",
                    "instance": str(credential["id"]),
                }
            },
        )
    ]


@pytest.mark.parametrize("argument", ["source", "instance"])
@pytest.mark.parametrize("as_external_agent", [False, True])
def test_the_caller_cannot_choose_the_source(
    db_conn: psycopg.Connection, argument: str, as_external_agent: bool
) -> None:
    with TestClient(app) as client:
        credential, agent_id = _client_and_agent(client)
        args: dict[str, Any] = {
            "agent_id": agent_id,
            "content": "must not insert",
            "as_external_agent": as_external_agent,
            argument: "user" if argument == "source" else "spoofed",
        }
        assert _failed(_tool_call(client, credential["token"], "send_message", args))
    assert _stored(db_conn, agent_id, "must not insert") == []
