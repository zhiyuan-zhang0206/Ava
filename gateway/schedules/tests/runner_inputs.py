"""Explicit SDK inputs for tests of the schedule execution engine."""

from contextlib import closing
from functools import partial

import ava
from ava.sdk_surface.process_context import process_clients
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.clock import Clock
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process.code_version import CodeVersion
from base.telemetry import process_name
from gateway.schedules import runner


def bind_actor(schedule_id: int, *, clients: ClientSet) -> None:
    ava.bind_context(
        AvaContext(
            identity=AgentIdentity(agent_id=None, owns_loop=True, actor=f"schedule:{schedule_id}"),
            clients=clients,
            clock_factory=Clock.from_settings,
        )
    )


def run_schedule(schedule_id: int, revision: int | None = None) -> int:
    image = ava.loaded_code_image()
    version = CodeVersion(image)
    gate = ProcessDbGate(process=process_name(), version=version.get)
    database = partial(Database.from_settings, gate=gate)
    with closing(process_clients(database=database)) as clients:
        return runner.run(
            schedule_id,
            revision,
            database=database,
            inputs=ScheduleInputs(database, clients.event_pipeline, image),
            bind_actor=partial(bind_actor, clients=clients),
            load_plugins=ava.ensure_plugins_loaded,
        )
