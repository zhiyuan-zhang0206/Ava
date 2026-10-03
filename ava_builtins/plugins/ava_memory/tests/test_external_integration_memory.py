"""An external attachment's memory writes use the borrowed identity and recheck the lease before any filesystem effect."""

from pathlib import Path
from typing import Annotated
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from pydantic import BaseModel, Field

import ava
from agent import state as state_module
from agent.extensions import registry as registry_module
from ava import agent_identity, external
from base.agents import impersonation as leases
from base.agents.messages.caller_identity import CallerIdentity
from base.cluster.machine import machine_name
from base.config import settings
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
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
    monkeypatch.setattr(state_module, "AgentState", state_module.AgentState)
    monkeypatch.setattr(agent_identity, "_external_identity", None)
    monkeypatch.setattr(agent_identity, "_agent_id", None)
    monkeypatch.setattr(ava, "state", None)
    monkeypatch.setattr(ava, "state_update", None)

    def loader_stub(**_kwargs: object) -> None:
        """Accept the `surface` kwarg attach passes (ignored)."""

    monkeypatch.setattr(ava, "ensure_plugins_loaded", loader_stub)
    extensions = ExtensionRegistry(
        (("integration", PluginContributions(state=(IntegrationPlugin,))),)
    )
    # The attachment builds its state class from the loaded plugins' registry; hand it ours.
    monkeypatch.setattr(registry_module, "build_registry", lambda: extensions)
    handle = state_module.PluginStateHandle(IntegrationPlugin, "integration")
    state_module.build_agent_state(extensions)

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


@pytest.mark.parametrize("store", ["personal", "shared"])
@pytest.mark.parametrize("stale_process_identity", [False, True])
def test_external_memory_write_uses_borrowed_identity(
    native_checkpoint: tuple[RuntimeIncarnation, state_module.PluginStateHandle[IntegrationPlugin]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    store: str,
    stale_process_identity: bool,
    database: Database,
    event_bus: EventBus,
) -> None:
    from ava_builtins.plugins.ava_memory import sdk as memory_sdk
    from base import paths

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
        database,
        event_bus,
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
    )
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(database, event_bus, lease["id"], owner)
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
    assert (
        leases.get(database, event_bus, lease["id"], attested_caller(lease))["delta_version"] == 0
    )


@pytest.mark.parametrize("operation", ["write", "note"])
def test_external_memory_rechecks_lease_before_filesystem_effects(
    native_checkpoint: tuple[RuntimeIncarnation, state_module.PluginStateHandle[IntegrationPlugin]],
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    database: Database,
    event_bus: EventBus,
) -> None:
    from ava_builtins.plugins.ava_memory import notes, sdk
    from base import paths

    def workspace(agent_id: int) -> Path:
        return tmp_path / str(agent_id)

    owner, _ = native_checkpoint
    monkeypatch.setattr(agent_identity, "_agent_id", owner.agent_id + 1000)
    monkeypatch.setattr(agent_identity, "_owns_loop", False)
    monkeypatch.setattr(paths, "workspace_dir", workspace)
    monkeypatch.setattr(notes, "workspace_dir", workspace)
    monkeypatch.setattr(settings.agent, "memory_per_agent_inject_enabled", True)
    lease = leases.request(
        database,
        event_bus,
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
    )
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(database, event_bus, lease["id"], owner)
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
                notes.per_agent_memory_note(AgentSlices.resolve())
        assert not list(tmp_path.iterdir())
    finally:
        with pytest.raises(leases.ImpersonationError, match="expired"):
            attachment.close()
