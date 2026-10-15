from typing import Any

from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog

__all__ = ["spawn_agent"]


def spawn_agent(
    *,
    machine: MachineIdentity,
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    spawner: str = "user",
    config: dict[str, object] | None = None,
    **kw: Any,
) -> int:
    from base.agents.birth.spawn import create_agent_row
    from base.agents.impersonation.wake import publish_inbound_wake

    db, bus = Database.from_settings(gate=database_gate), EventBus.from_settings()
    agent_id, _, _prompt_id, _attempt_id = create_agent_row(
        db,
        bus,
        spawner=spawner,
        machine=machine.name(),
        config=config,
        catalog=catalog,
        authority=authority,
        **kw,
    )
    publish_inbound_wake(db, bus, agent_id, "0")
    return agent_id
