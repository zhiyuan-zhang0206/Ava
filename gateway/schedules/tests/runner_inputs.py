"""Explicit SDK inputs for tests of the schedule execution engine."""

import ava
from ava.sdk_surface.process_context import process_clients
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from gateway.schedules import runner


def bind_actor(schedule_id: int) -> None:
    ava.bind_context(
        AvaContext(
            identity=AgentIdentity(agent_id=None, owns_loop=True, actor=f"schedule:{schedule_id}"),
            clients=process_clients(),
        )
    )


def run_schedule(schedule_id: int, revision: int | None = None) -> int:
    return runner.run(
        schedule_id, revision, bind_actor=bind_actor, load_plugins=ava.ensure_plugins_loaded
    )
