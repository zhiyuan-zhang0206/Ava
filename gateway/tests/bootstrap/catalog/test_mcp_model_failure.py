from collections.abc import Callable, Iterator

import psycopg
import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.tests.configuration import snapshot_process_config
from base.packages.tests.lm.providers.provider_plugin_support import (
    provider_plugin as provider_plugin,
)
from base.paths import plugins_dir
from gateway.tests.mcp_calls import create_token, post_message, tool_call, tool_result
from tests.fixtures.gateway_config import GatewayTestClient, gateway_test_client, spawn_via_http


@pytest.fixture
def failed_provider_agent(
    provider_plugin: Callable[..., None], *, machine_identity: MachineIdentity
) -> Iterator[tuple[GatewayTestClient, int]]:
    boot = snapshot_process_config()
    boot.set_field("mcp_endpoint_enabled", True)
    with gateway_test_client(machine=machine_identity, boot=boot) as client:
        born = spawn_via_http(client, {"prompt": "retain this agent", "prompt_source": "user"})
        assert born.status_code == 201, born.text
        agent_id = born.json()["id"]

    provider_plugin(dir_name="z_broken_provider")
    (plugins_dir() / "z_broken_provider" / "provider.py").write_text(
        'raise RuntimeError("provider private diagnostic")\n'
    )
    with gateway_test_client(machine=machine_identity, boot=boot) as client:
        yield client, agent_id


def test_provider_failure_keeps_mcp_reads(
    failed_provider_agent: tuple[GatewayTestClient, int],
) -> None:
    client, agent_id = failed_provider_agent
    token = create_token(client)
    tools = post_message(client, token, {"jsonrpc": "2.0", "id": 0, "method": "tools/list"})
    assert len(tools[0]["result"]["tools"]) == 7
    listed = tool_result(tool_call(client, token, "list_agents", {}))
    assert [card["agent_id"] for card in listed["agents"]] == [agent_id]
    assert listed["agents"][0]["supports_vision"] is None
    detail = tool_result(tool_call(client, token, "get_agent", {"agent_id": agent_id}))
    assert detail["agent_id"] == agent_id and detail["supports_vision"] is None


def test_provider_failure_blocks_new_model_work(
    failed_provider_agent: tuple[GatewayTestClient, int], db_conn: psycopg.Connection
) -> None:
    client, _agent_id = failed_provider_agent
    token = create_token(client)
    result = tool_call(
        client,
        token,
        "spawn_agent_guarded_v1",
        {"prompt": "new", "idempotency_key": "blocked-model"},
    )
    assert result["result"]["isError"] is True
    assert "Model catalog is unavailable" in tool_result(result)
    assert "provider private diagnostic" not in tool_result(result)
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)


def test_provider_failure_preserves_mcp_read_scope(
    failed_provider_agent: tuple[GatewayTestClient, int], db_conn: psycopg.Connection
) -> None:
    client, agent_id = failed_provider_agent
    read_token = create_token(client, name="read-only-during-failure", scope="read")
    read_detail = tool_result(tool_call(client, read_token, "get_agent", {"agent_id": agent_id}))
    assert read_detail["supports_vision"] is None
    refused = tool_call(
        client, read_token, "send_message", {"agent_id": agent_id, "content": "forbidden"}
    )
    assert refused["result"]["isError"] is True
    assert "requires write scope" in tool_result(refused)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE content='forbidden'"
    ).fetchone() == (0,)


def test_provider_failure_keeps_mcp_recovery_tools(
    failed_provider_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection,
    *,
    machine_identity: MachineIdentity,
) -> None:
    client, agent_id = failed_provider_agent
    token = create_token(client)
    sent = tool_result(
        tool_call(client, token, "send_message", {"agent_id": agent_id, "content": "still here"})
    )
    assert sent["status"] in {"queued", "claimed", "idling"}
    messages = tool_result(tool_call(client, token, "get_messages", {"agent_id": agent_id}))
    assert isinstance(messages["messages"], list)
    stopped = tool_result(tool_call(client, token, "terminate_agent", {"agent_id": agent_id}))
    assert stopped["status"] == "enqueued"
    machine_identity.set(role="gateway")
    status = tool_result(tool_call(client, token, "cluster_status", {}))
    assert status["machine_name"]
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)
