from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
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
from ava import impersonation
from ava.boot import process_clients
from ava.sdk_surface import process_context
from ava.sdk_surface.installation import Installation
from ava.tests.fixtures.pin_agent import pin_agent, pin_no_identity
from base.agents import impersonation as leases
from base.agents.impersonation.tests.impersonation_support import recorded_tree
from base.api_contracts.caller_identity import CallerIdentity
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.machine import MachineIdentity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import snapshot_process_config
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from base.packages.plugins.loading import registry as registry_module


def _union(left: set[str], right: set[str]) -> set[str]:
    return left | right


class IntegrationPlugin(BaseModel):
    seen: Annotated[set[str], _union] = Field(default_factory=set)


@pytest.fixture
def native_checkpoint(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    model_installation: Installation,
    machine_identity: MachineIdentity,
) -> Iterator[tuple[RuntimeIncarnation, state_module.PluginStateHandle[IntegrationPlugin]]]:
    pin_no_identity()
    process_context.unbind_exec_turn()
    request.addfinalizer(process_context.unbind_exec_turn)

    def loader_stub(**_kwargs: object) -> None:
        pass

    monkeypatch.setattr(ava, "ensure_plugins_loaded", loader_stub)
    extensions = ExtensionRegistry(
        (("integration", PluginContributions(state=(IntegrationPlugin,))),)
    )
    installation = replace(model_installation, registry=extensions, faces=True)
    monkeypatch.setattr(ava, "__plugin_installation__", installation, raising=False)

    def build_registry(*, validate_state: object = None) -> ExtensionRegistry:
        return extensions

    monkeypatch.setattr(registry_module, "build_registry", build_registry)
    handle = state_module.PluginStateHandle(IntegrationPlugin, "integration")
    state_module.build_agent_state(extensions)

    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, machine_identity.name(), owner.generation, owner.owner),
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
    config = snapshot_process_config()
    with closing(process_clients(config=config, machine=machine_identity)) as clients:
        pin_agent(
            None,
            owns_loop=False,
            clients=clients,
            machine=machine_identity,
            clock_factory=lambda: Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
        )
        yield owner, handle


@pytest.mark.parametrize("stale_process_identity", [False, True])
def test_external_personal_path_uses_borrowed_identity(
    native_checkpoint: tuple[RuntimeIncarnation, state_module.PluginStateHandle[IntegrationPlugin]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stale_process_identity: bool,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from ava_builtins.plugins.ava_memory import sdk as memory_sdk
    from base import paths

    def workspace(agent_id: int) -> Path:
        return tmp_path / str(agent_id)

    owner, _ = native_checkpoint
    stale_id = owner.agent_id + 1000
    pin_agent(stale_id if stale_process_identity else None, owns_loop=False)
    monkeypatch.setitem(vars(ava), "memory", memory_sdk)
    monkeypatch.setattr(paths, "workspace_dir", workspace)
    lease = leases.request(
        database,
        event_bus,
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        authority=config_authority,
        local_machine=machine_identity.name(),
    )
    leases.accept(
        database,
        event_bus,
        lease["id"],
        owner.agent_id,
        owner,
        "Handoff brief",
        local_machine=machine_identity.name(),
    )
    leases.activate(database, event_bus, lease["id"], owner)

    with impersonation.attach(lease["id"]):
        personal = ava.memory.PERSONAL_PATH

    assert personal == tmp_path / str(owner.agent_id) / "memory"
    assert not (tmp_path / str(stale_id)).exists()


@pytest.mark.parametrize("operation", ["personal_path", "note"])
def test_external_memory_rechecks_lease_before_filesystem_effects(
    native_checkpoint: tuple[RuntimeIncarnation, state_module.PluginStateHandle[IntegrationPlugin]],
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from ava_builtins.plugins.ava_memory import notes, sdk
    from base import paths

    def workspace(agent_id: int) -> Path:
        return tmp_path / str(agent_id)

    owner, _ = native_checkpoint
    pin_agent(owner.agent_id + 1000, owns_loop=False)
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
        authority=config_authority,
        local_machine=machine_identity.name(),
    )
    leases.accept(
        database,
        event_bus,
        lease["id"],
        owner.agent_id,
        owner,
        "Handoff brief",
        local_machine=machine_identity.name(),
    )
    leases.activate(database, event_bus, lease["id"], owner)
    attachment = impersonation.attach(lease["id"])
    try:
        db_conn.execute(
            "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE id=%s",
            (lease["id"],),
        )
        db_conn.commit()
        with pytest.raises(leases.ImpersonationError, match="expired"):
            if operation == "personal_path":
                _ = sdk.PERSONAL_PATH
            else:
                context = replace(
                    ava.context,
                    agent=AgentSlices.resolve(
                        default_reader=lambda _domain, field: config_authority.service_field_value(
                            field
                        )
                    ),
                )
                notes.per_agent_memory_note(context)
        assert not list(tmp_path.iterdir())
    finally:
        with pytest.raises(leases.ImpersonationError, match="expired"):
            attachment.close()
