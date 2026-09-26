"""Real PostgreSQL consent, checkpoint hydration, and external SDK effects."""

from pathlib import Path
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
from ava import agent_identity, external
from ava.external_state import decode_plugin_delta, load_snapshot
from shared.agents import impersonation as leases
from shared.agents.impersonation import impersonation_history as history
from shared.caller_identity import CallerIdentity
from shared.config import settings
from shared.db import create_agent
from shared.machine import machine_name
from shared.plugin_context import PluginContext
from shared.runtime_incarnation import RuntimeIncarnation
from tests.impersonation_support import attested_caller, recorded_tree


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
    monkeypatch.setattr(agent_identity, "_external_identity", None)
    monkeypatch.setattr(agent_identity, "_agent_id", None)
    monkeypatch.setattr(ava, "state", None)
    monkeypatch.setattr(ava, "state_update", None)

    def loader_stub(**_kwargs: object) -> None:
        """Accept the `surface` kwarg attach passes (ignored)."""

    monkeypatch.setattr(ava, "_ensure_plugins_loaded", loader_stub)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, handle = native_checkpoint
    agent_id = owner.agent_id

    lease = leases.request(
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
    )
    leases.accept(lease["id"], agent_id, owner, "Handoff brief")
    leases.activate(lease["id"], owner)
    monkeypatch.setattr(external, "process_metadata", lambda: attested_caller(lease))
    with external.attach(lease["id"]):
        assert agent_id == ava.self.AGENT_ID
        assert agent_identity.require_actor() == f"agent:{agent_id}"
        assert ava.state.messages[0].content == "Native task"
        assert handle.read().seen == {"native"}
        handle.update({"seen": {"external"}})
    updated = leases.get(lease["id"], attested_caller(lease))
    assert updated["delta_version"] == 1
    assert decode_plugin_delta(updated["plugin_delta"][0]) == {"integration__seen": {"external"}}
    native_snapshot, _, _ = load_snapshot(agent_id)
    assert native_snapshot.integration__seen == {"native"}
    with external.attach(lease["id"]):
        assert handle.read().seen == {"native", "external"}
    assert leases.get(lease["id"], attested_caller(lease))["delta_version"] == 1


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
    lease = leases.request(
        owner.agent_id,
        caller=caller,
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
    )
    leases.accept(lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(lease["id"], owner)
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
        lease["id"], attested_caller(lease), "Delivered the implementation to the peer"
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


def test_v1_attachment_send_message_certifies_from_the_central_receipt(
    gateway_unit: TestClient,
    native_checkpoint: tuple[RuntimeIncarnation, state_module.PluginStateHandle[IntegrationPlugin]],
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attachment-owned ``ava.agents.send_message`` completes only on exact replay."""
    import httpx

    from ava import _impersonation_events as reader
    from services.agent_host.impersonation_events import reconcile_one

    owner, _ = native_checkpoint
    monkeypatch.setattr(settings.general, "impersonation_event_manifest_enabled", True)
    monkeypatch.setattr(
        settings.general,
        "impersonation_event_manifest_certification_secret",
        "attachment-manifest-certification-secret-000001",
    )
    monkeypatch.setattr("ava._gateway_transport._client", gateway_unit)
    peer_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,lease_expires_at) "
        "VALUES(%s,'idling',%s,clock_timestamp()+interval '10 minutes')",
        (peer_id, machine_name()),
    )
    db_conn.commit()
    lease = leases.request(
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        automatic=True,
        name="attachment-manifest",
        executor_name="codex",
    )
    leases.accept(lease["id"], owner.agent_id, owner, "Send through the attachment")
    leases.activate(lease["id"], owner)
    monkeypatch.setattr(external, "process_metadata", lambda: attested_caller(lease))
    with external.attach(lease["id"]):
        ava.agents.send_message(peer_id, "Manifest-backed attachment send")
    leases.release(lease["id"], attested_caller(lease), "Sent the peer update")

    expected = db_conn.execute(
        "SELECT event_key,line_sha256,event_kind,event_at FROM "
        "agent_impersonation_event_participant_items WHERE lease_id=%s UNION ALL "
        "SELECT event_key,line_sha256,event_kind,event_at FROM "
        "agent_impersonation_event_expected_items WHERE lease_id=%s ORDER BY event_kind",
        (lease["id"], lease["id"]),
    ).fetchall()
    # The attachment-local SDK meter may be disabled by the surrounding
    # process profile, but the borrowed inter-agent send is always a central
    # audit receipt and must remain certifiable with the full observed union.
    assert any(row[2] == "api_event" for row in expected)

    def get(_path: str, *, params: dict[str, Any]) -> httpx.Response:
        wanted_kind = "sdk_call" if params.get("event_name") == "sdk_call" else "api_event"
        items: list[dict[str, Any]] = []
        for key, digest, kind, event_at in expected:
            if kind != wanted_kind:
                continue
            is_sdk = kind == "sdk_call"
            items.append(
                {
                    "id": key.removeprefix("event:"),
                    "line_sha256": digest,
                    "event_name": "sdk_call" if is_sdk else "send_message",
                    "category": "sdk" if is_sdk else "audit",
                    "ts": event_at.isoformat(),
                    "agent_id": owner.agent_id if is_sdk else peer_id,
                    "source": "self" if is_sdk else f"agent:{owner.agent_id}",
                    "attributes": {
                        "impersonation_session": f"{owner.agent_id}:0",
                        **({"fn": "ava.agents.send_message", "duration": 0.0} if is_sdk else {}),
                    },
                }
            )
        return httpx.Response(
            200,
            request=httpx.Request("GET", "http://manifest.test/api/events"),
            json={"items": items, "meta": {"has_more": False}},
        )

    monkeypatch.setattr(reader, "_get", get)
    reconcile_one()
    certified = history.resolve(owner.agent_id, 0)
    assert certified["events_completed_at"] is not None
    assert certified["event_delivery_pending_reason"] is None


@pytest.mark.parametrize("store", ["personal", "shared"])
@pytest.mark.parametrize("stale_process_identity", [False, True])
def test_external_memory_write_uses_borrowed_identity(
    native_checkpoint: tuple[RuntimeIncarnation, state_module.PluginStateHandle[IntegrationPlugin]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    store: str,
    stale_process_identity: bool,
) -> None:
    from ava_builtins.plugins.ava_memory import sdk as memory_sdk
    from shared import paths

    def workspace(agent_id: int) -> Path:
        return tmp_path / str(agent_id)

    owner, _ = native_checkpoint
    stale_id = owner.agent_id + 1000
    monkeypatch.setattr(agent_identity, "_agent_id", stale_id if stale_process_identity else None)
    monkeypatch.setattr(agent_identity, "_owns_loop", False)
    monkeypatch.setitem(vars(ava), "memory", memory_sdk)
    monkeypatch.setattr(paths, "workspace_dir", workspace)
    monkeypatch.setattr(paths, "memory_dir", lambda: tmp_path / "shared")
    lease = leases.request(
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
    )
    leases.accept(lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(lease["id"], owner)
    monkeypatch.setattr(external, "process_metadata", lambda: attested_caller(lease))

    with external.attach(lease["id"]):
        entry = ava.memory.write("working-rule", "Verify current state.\n", store=store)

    root = tmp_path / "shared" if store == "shared" else tmp_path / str(owner.agent_id) / "memory"
    assert entry == root / "working-rule.md"
    assert entry.read_text().endswith("Verify current state.\n")
    assert (root / "MEMORY.md").read_text() == "- [working-rule](working-rule.md) — working-rule\n"
    assert not (tmp_path / str(stale_id)).exists()
    if store == "shared":
        assert f"ava_agent: {owner.agent_id}\n" in entry.read_text()
    # Memory files are immediate SDK effects, separate from checkpoint deltas.
    assert leases.get(lease["id"], attested_caller(lease))["delta_version"] == 0


@pytest.mark.parametrize("operation", ["write", "note"])
def test_external_memory_rechecks_lease_before_filesystem_effects(
    native_checkpoint: tuple[RuntimeIncarnation, state_module.PluginStateHandle[IntegrationPlugin]],
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    from ava_builtins.plugins.ava_memory import notes, sdk
    from shared import paths

    def workspace(agent_id: int) -> Path:
        return tmp_path / str(agent_id)

    owner, _ = native_checkpoint
    monkeypatch.setattr(agent_identity, "_agent_id", owner.agent_id + 1000)
    monkeypatch.setattr(agent_identity, "_owns_loop", False)
    monkeypatch.setattr(paths, "workspace_dir", workspace)
    monkeypatch.setattr(notes, "workspace_dir", workspace)
    monkeypatch.setattr(settings.agent, "memory_per_agent_inject_enabled", True)
    lease = leases.request(
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
    )
    leases.accept(lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(lease["id"], owner)
    monkeypatch.setattr(external, "process_metadata", lambda: attested_caller(lease))
    attachment = external.attach(lease["id"])
    try:
        db_conn.execute(
            "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE id=%s",
            (lease["id"],),
        )
        db_conn.commit()
        with pytest.raises(leases.ImpersonationError, match="expired"):
            if operation == "write":
                sdk.write("expired-note", "Must not be written.")
            else:
                notes.per_agent_memory_note()
        assert not list(tmp_path.iterdir())
    finally:
        with pytest.raises(leases.ImpersonationError, match="expired"):
            attachment.close()
