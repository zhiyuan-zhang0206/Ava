from contextlib import closing
from functools import partial

import meiva
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.clock import Clock
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentity
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process.code_version import CodeVersion
from base.telemetry import process_name
from meiva.boot import process_clients
from meiva.sdk_surface import process_context
from services.wake.schedule_manager import engine


def bind_actor(schedule_id: int, *, clients: ClientSet, machine: MachineIdentity) -> None:
    process_context.bind_context(
        MeivaContext(
            identity=AgentIdentity(agent_id=None, owns_loop=True, actor=f"schedule:{schedule_id}"),
            clients=clients,
            machine=machine,
            clock_factory=Clock.from_settings,
        )
    )


def run_schedule(schedule_id: int, revision: int | None = None, *, machine: MachineIdentity) -> int:
    image = process_context.loaded_code_image()
    version = CodeVersion(image)
    gate = ProcessDbGate(process=process_name(), version=version.get)
    database = partial(
        Database.from_settings,
        gate=gate,
        direct_url=ClusterDirectUrl(machine.host, gateway_url_reader=lambda: ""),
    )
    with closing(process_clients(database=database, machine=machine)) as clients:
        return engine.run(
            schedule_id,
            revision,
            database=database,
            inputs=ScheduleInputs(database, clients.event_pipeline, image, machine=machine),
            bind_actor=partial(bind_actor, clients=clients, machine=machine),
            load_plugins=meiva.ensure_plugins_loaded,
        )
