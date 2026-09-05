"""Real PostgreSQL consent, checkpoint hydration, and external plugin journaling."""

from typing import Annotated, Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from pydantic import BaseModel, Field

import ava
from agent import state as state_module
from ava import _boot, external
from ava._external_state import decode_plugin_delta, load_snapshot
from shared import impersonation as leases
from shared.caller_identity import CallerIdentity
from shared.config import settings
from shared.db import create_agent
from shared.machine import machine_name
from shared.plugin_context import PluginContext
from shared.runtime_incarnation import RuntimeIncarnation


def _union(left: set[str], right: set[str]) -> set[str]:
    return left | right


class IntegrationPlugin(BaseModel):
    seen: Annotated[set[str], _union] = Field(default_factory=set)


@pytest.fixture
def native_checkpoint(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> tuple[RuntimeIncarnation, state_module.PluginStateHandle[IntegrationPlugin]]:
    registrations: tuple[tuple[str, Any], ...] = (
        ("_EXTRA_FIELDS", {}),
        ("_PLUGIN_NAMESPACE_FIELDS", {}),
        ("_PLUGIN_STATE_CLASSES", set[type[BaseModel]]()),
        ("_BASE_FIELD_DECLARED", set[str]()),
    )
    for name, value in registrations:
        monkeypatch.setattr(state_module, name, value)
    monkeypatch.setattr(state_module, "AgentState", state_module.AgentState)
    monkeypatch.setattr(_boot, "_external_identity", None)
    monkeypatch.setattr(_boot, "_agent_id", None)
    monkeypatch.setattr(ava, "state", None)
    monkeypatch.setattr(ava, "state_update", None)
    monkeypatch.setattr(ava, "_ensure_plugins_loaded", lambda: None)
    with PluginContext("integration"):
        handle = state_module.register_plugin_state(IntegrationPlugin)
    state_module.build_agent_state()

    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, machine_name(), owner.generation, owner.owner),
    )
    db_conn.commit()
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "messages": [HumanMessage(content="Native task")],
        "integration__seen": {"native"},
    }
    versions: dict[str, str | int | float] = {"messages": "1", "integration__seen": "1"}
    checkpoint["channel_versions"] = versions
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        saver.setup()
        saver.put(
            {"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint,
            {"source": "input", "step": 1, "parents": {}},
            versions,
        )
    return owner, handle


def test_external_attach_reads_native_checkpoint_and_only_journals_delta(
    native_checkpoint: tuple[RuntimeIncarnation, state_module.PluginStateHandle[IntegrationPlugin]],
) -> None:
    owner, handle = native_checkpoint
    agent_id = owner.agent_id

    lease = leases.request(agent_id, caller=CallerIdentity(kind="external_agent", subject="codex"))
    leases.accept(lease["id"], agent_id, owner)
    leases.activate(lease["id"], owner)
    with external.attach(lease["id"], token=lease["token"]):
        assert agent_id == ava.self.AGENT_ID
        assert _boot.require_actor() == f"agent:{agent_id}"
        assert ava.state.messages[0].content == "Native task"
        assert handle.read().seen == {"native"}
        handle.update({"seen": {"external"}})
    updated = leases.get(lease["id"], lease["token"])
    assert updated["delta_version"] == 1
    assert decode_plugin_delta(updated["plugin_delta"][0]) == {"integration__seen": {"external"}}
    native_snapshot, _, _ = load_snapshot(agent_id)
    assert native_snapshot.integration__seen == {"native"}
    with external.attach(lease["id"], token=lease["token"]):
        assert handle.read().seen == {"native", "external"}
    assert leases.get(lease["id"], lease["token"])["delta_version"] == 1


def test_borrowed_sender_reaches_peer_through_gateway_and_returns_real_provenance(
    gateway_unit: TestClient,
    native_checkpoint: tuple[RuntimeIncarnation, state_module.PluginStateHandle[IntegrationPlugin]],
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, _ = native_checkpoint
    monkeypatch.setattr("ava._gateway_transport._client", gateway_unit)
    monkeypatch.setenv(
        "AVA_CALLER_IDENTITY", '{"kind":"external_agent","subject":"codex","instance":"test"}'
    )
    peer_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,lease_expires_at) "
        "VALUES(%s,'idling',%s,clock_timestamp()+interval '10 minutes')",
        (peer_id, machine_name()),
    )
    db_conn.commit()
    caller = CallerIdentity(kind="external_agent", subject="codex", instance="test")
    lease = leases.request(owner.agent_id, caller=caller)
    leases.accept(lease["id"], owner.agent_id, owner)
    leases.activate(lease["id"], owner)

    with external.attach(lease["id"], token=lease["token"]):
        ava.agents.send_message(peer_id, "Implementation ready for review")

    outbound = db_conn.execute(
        "SELECT content,kind,source,status FROM inbound_messages WHERE agent_id=%s", (peer_id,)
    ).fetchall()
    assert outbound == [
        ("Implementation ready for review", "chat", f"agent:{owner.agent_id}", "pending")
    ]
    db_conn.commit()
    returned = leases.release(
        lease["id"], lease["token"], "Delivered the implementation to the peer"
    )
    handoff = db_conn.execute(
        "SELECT agent_id,content,source,payload FROM inbound_messages WHERE id=%s",
        (returned["summary_inbound_id"],),
    ).fetchone()
    assert handoff is not None
    assert handoff[:3] == (
        owner.agent_id,
        "Delivered the implementation to the peer",
        "external_agent:codex:test",
    )
    assert handoff[3]["caller_identity"] == caller.model_dump()
