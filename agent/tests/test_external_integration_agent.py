"""Real PostgreSQL consent, checkpoint hydration and the external SDK effects of an attachment: native checkpoint reads, borrowed sender and lease-log recording."""

from typing import Annotated, Any, cast
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from pydantic import BaseModel, Field

import ava
from agent import state as state_module
from ava import agent_identity, external
from ava.external.state import decode_plugin_delta, load_snapshot
from base.agents import impersonation as leases
from base.agents.impersonation import history
from base.agents.messages.caller_identity import CallerIdentity
from base.cluster.machine import machine_name
from base.config import settings
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from tests.impersonation_support import attested_caller, recorded_tree


def _union(left: set[str], right: set[str]) -> set[str]:
    return left | right


class IntegrationPlugin(BaseModel):
    seen: Annotated[set[str], _union] = Field(default_factory=set)


@pytest.fixture
def native_checkpoint(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> tuple[RuntimeIncarnation, state_module.PluginStateHandle[IntegrationPlugin]]:
    registry = ExtensionRegistry(
        (("integration", PluginContributions(state=(IntegrationPlugin,))),)
    )
    # The attachment builds its state class from the registry of the plugins loaded into the process.
    monkeypatch.setattr("agent.extensions.registry.build_registry", lambda: registry)
    monkeypatch.setattr(agent_identity, "_external_identity", None)
    monkeypatch.setattr(agent_identity, "_agent_id", None)
    monkeypatch.setattr(ava, "state", None)
    monkeypatch.setattr(ava, "state_update", None)

    def loader_stub(**_kwargs: object) -> None:
        """Accept the `surface` kwarg attach passes (ignored)."""

    monkeypatch.setattr(ava, "ensure_plugins_loaded", loader_stub)
    handle = state_module.PluginStateHandle(IntegrationPlugin, "integration")
    state_module.build_agent_state(registry)

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
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    owner, handle = native_checkpoint
    agent_id = owner.agent_id

    lease = leases.request(
        database,
        event_bus,
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
    )
    leases.accept(database, event_bus, lease["id"], agent_id, owner, "Handoff brief")
    leases.activate(database, event_bus, lease["id"], owner)
    monkeypatch.setattr(external, "process_metadata", lambda: attested_caller(lease))
    with external.attach(lease["id"]):
        assert agent_id == ava.self.AGENT_ID
        assert agent_identity.require_actor() == f"agent:{agent_id}"
        assert ava.state.messages[0].content == "Native task"
        assert handle.read().seen == {"native"}
        handle.update({"seen": {"external"}})
    updated = leases.get(database, event_bus, lease["id"], attested_caller(lease))
    assert updated["delta_version"] == 1
    native_snapshot, _, _ = load_snapshot(agent_id)
    assert decode_plugin_delta(updated["plugin_delta"][0], cast(Any, type(native_snapshot))) == {
        "integration__seen": {"external"}
    }
    assert native_snapshot.integration__seen == {"native"}
    with external.attach(lease["id"]):
        assert handle.read().seen == {"native", "external"}
    assert (
        leases.get(database, event_bus, lease["id"], attested_caller(lease))["delta_version"] == 1
    )


@pytest.mark.usefixtures("sdk_via_gateway")
def test_borrowed_sender_reaches_peer_through_gateway_and_returns_real_provenance(
    native_checkpoint: tuple[RuntimeIncarnation, state_module.PluginStateHandle[IntegrationPlugin]],
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    owner, _ = native_checkpoint
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
    lease = leases.request(
        database,
        event_bus,
        owner.agent_id,
        caller=caller,
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
    )
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(database, event_bus, lease["id"], owner)
    monkeypatch.setattr(external, "process_metadata", lambda: attested_caller(lease))

    with external.attach(lease["id"]):
        ava.agents.send_message(peer_id, "Implementation ready for review")

    outbound = db_conn.execute(
        "SELECT content,kind,source,status FROM inbound_messages WHERE agent_id=%s", (peer_id,)
    ).fetchall()
    assert outbound == [
        ("Implementation ready for review", "chat", f"agent:{owner.agent_id}", "pending")
    ]
    db_conn.commit()
    returned = leases.release(
        database,
        event_bus,
        lease["id"],
        attested_caller(lease),
        "Delivered the implementation to the peer",
    )
    handoff = db_conn.execute(
        "SELECT agent_id,content,source,payload FROM inbound_messages WHERE id=%s",
        (returned["summary_inbound_id"],),
    ).fetchone()
    assert handoff is not None
    assert handoff[:3] == (
        owner.agent_id,
        f"External session ended (lease {lease['id']}).\n\nDelivered the implementation to the peer",
        "external_agent:codex:test",
    )
    assert handoff[3]["caller_identity"] == caller.model_dump()


@pytest.mark.usefixtures("sdk_via_gateway")
def test_attachment_send_message_is_recorded_in_the_lease_log_and_completes(
    native_checkpoint: tuple[RuntimeIncarnation, state_module.PluginStateHandle[IntegrationPlugin]],
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    """Attachment-owned ``ava.agents.send_message`` is a central audit row in the lease log."""
    owner, _ = native_checkpoint
    peer_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,lease_expires_at) "
        "VALUES(%s,'idling',%s,clock_timestamp()+interval '10 minutes')",
        (peer_id, machine_name()),
    )
    db_conn.commit()
    lease = leases.request(
        database,
        event_bus,
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        automatic=True,
        name="attachment-event-log",
        executor_name="codex",
    )
    leases.accept(
        database, event_bus, lease["id"], owner.agent_id, owner, "Send through the attachment"
    )
    leases.activate(database, event_bus, lease["id"], owner)
    monkeypatch.setattr(external, "process_metadata", lambda: attested_caller(lease))
    with external.attach(lease["id"]):
        ava.agents.send_message(peer_id, "Log-backed attachment send")
    leases.release(database, event_bus, lease["id"], attested_caller(lease), "Sent the peer update")

    # The attachment-local SDK meter may be disabled by the surrounding process
    # profile, but the borrowed inter-agent send is always a central audit row.
    api_events = [
        row["payload"]
        for row in history.entries(str(lease["id"]), db_conn)
        if row["kind"] == "api_event"
    ]
    assert [(event["event_name"], event["agent_id"]) for event in api_events] == [
        ("send_message", peer_id)
    ]
    completed = history.resolve(database, owner.agent_id, 0)
    assert completed["events_completed_at"] is not None
    assert completed["event_delivery_pending_reason"] is None
