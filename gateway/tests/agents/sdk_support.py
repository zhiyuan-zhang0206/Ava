"""Shared real-route SDK apparatus and SQL setup for Gateway agent tests."""

from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

import ava
from ava.gateway_client.transport import use_client
from ava.sdk_surface.install import Installation
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.plugin_providers import build_model_catalog
from tests.fixtures.configuration import snapshot_process_config


def spawn_agent(*, config_authority: ConfigAuthority, database_gate: ProcessDbGate) -> int:
    """Setup helper — a row for the SDK's self identity (Task #1236 split: the
    row is created by create_agent_row; nothing launches, these tests only need
    the row to exist)."""
    from base.cluster.machine import machine_name
    from ops.agents.spawn import create_agent_row

    agent_id, _, _prompt_id, _attempt_id = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        machine=machine_name(),
        catalog=build_model_catalog(),
        authority=config_authority,
    )
    return agent_id


@pytest.fixture(autouse=True)
def sdk_via_gateway(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    model_installation: Installation,
    config_authority: ConfigAuthority,
):
    """SDK ↔ Gateway path in-process test apparatus:
    1. monkeypatch session noop — spawn / resurrect / respawn don't really start child python
    2. TestClient(app) starts lifespan (build db_pool etc.), mount it as ava SDK's
       httpx client — SDK calls go through ASGI directly into gateway endpoint, real DB real logic,
       not bound to TCP port
    """
    monkeypatch.setattr(
        ava,
        "__plugin_installation__",
        replace(model_installation, authority=config_authority),
        raising=False,
    )
    from base.cluster import machines as _machines
    from base.cluster.machine import machine_name
    from gateway.agents import forward as _agents_forward_router
    from gateway.agents import router as _agents_router
    from gateway.app import app
    from ops.lifecycle import launch_agent_op, lifecycle_op
    from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent

    # POST /api/agents always forwards the launch to a runner's ops server over
    # HTTP, even for the co-located box. There is no live ops server in-process,
    # so stand in for the runner's ops daemon: dispatch launch_agent_op in-process
    # against the gateway's db_pool (exactly what the daemon does on receiving the
    # forwarded op), so the SDK spawn yields a real local agent row.
    async def _in_process_forward(
        _db: object, target: str, body: LaunchAgentRequest
    ) -> SpawnedAgent:
        return await launch_agent_op(
            database, event_bus, body, app.state.db_pool, catalog=build_model_catalog()
        )

    # Same pattern for lifecycle ops (terminate / resurrect / restart): the
    # runner's ops daemon dispatches lifecycle_op in-process; mirror that here
    # so a forwarded local lifecycle call executes against the test DB.
    async def _in_process_lifecycle(
        _db: object, target: str, path: str, json_body: dict[str, Any]
    ) -> dict[str, Any]:
        # model_dump mirrors the daemon serializing the response model onto the wire.
        return (
            await lifecycle_op(
                database,
                event_bus,
                path,
                json_body,
                app.state.db_pool,
                catalog=build_model_catalog(),
            )
        ).model_dump(mode="json")

    # post_agents reads the target's capability from the registry; the SDK targets
    # the local machine, so resolve it to agent-runner as register_self would.
    real_lookup_role = _machines.lookup_role

    def _lookup_role(_db: Database, name: str) -> list[str]:
        if name == machine_name():
            return ["gateway", "agent-runner"]
        return real_lookup_role(_db, name)

    # The spawn preflight also reads the pause latch for the same target; the
    # local machine is never paused in tests, so stub it alongside the role.
    real_is_paused = _machines.is_paused

    def _is_paused(_db: Database, name: str) -> bool:
        if name == machine_name():
            return False
        return real_is_paused(_db, name)

    # Mock all API keys so spawn validation passes — these tests exercise
    # the full gateway spawn path, which validates model config before forwarding.
    from pydantic import SecretStr

    from base.config import settings as _settings

    for _attr in (
        "anthropic_api_key",
        "deepseek_api_key",
        "gemini_api_key",
        "openai_api_key",
        "xiaomi_api_key",
        "moonshot_api_key",
        "zhipu_api_key",
        "dashscope_api_key",
    ):
        monkeypatch.setattr(_settings.lm, _attr, SecretStr("sk-test"))

    monkeypatch.setattr(_settings.data_plane, "cluster_secret", "sdk-test-secret")
    monkeypatch.setattr(_settings.gateway, "auth_middleware_enabled", True)
    from gateway import app as gateway_app

    monkeypatch.setattr(gateway_app, "ConfigBoot", snapshot_process_config)
    with (
        TestClient(
            app,
            base_url="http://test-gateway",
            headers={"Authorization": "Bearer sdk-test-secret"},
        ) as tc,
        use_client(tc),
    ):
        monkeypatch.setattr(_agents_router, "forward_spawn_to_remote", _in_process_forward)
        monkeypatch.setattr(_agents_forward_router, "enqueue_lifecycle", _in_process_lifecycle)
        monkeypatch.setattr(_machines, "lookup_role", _lookup_role)
        monkeypatch.setattr(_machines, "is_paused", _is_paused)
        yield


def inbound_rows(db: psycopg.Connection, agent_id: int) -> list[tuple]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages "
            "WHERE agent_id = %s ORDER BY id ASC",
            (agent_id,),
        )
        return cur.fetchall()
