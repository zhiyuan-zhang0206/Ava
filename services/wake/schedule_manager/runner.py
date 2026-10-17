# meiva:ref services/wake/schedule_manager/docs/runner-composition.meiva.okf.md#runner-revision-admission

from __future__ import annotations

import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path

from loguru import logger

import meiva
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.native_process.code_version import CodeVersion
from base.paths import prod_service_checkout_error
from base.telemetry import process_name
from meiva.boot import process_clients
from meiva.sdk_surface import process_context
from services.wake.schedule_manager import engine


def _bind_schedule_actor(
    schedule_id: int,
    *,
    clients: ClientSet,
    clock_factory: Callable[[], Clock],
    machine: MachineIdentity,
) -> None:
    process_context.bind_context(
        MeivaContext(
            identity=AgentIdentity(agent_id=None, owns_loop=True, actor=f"schedule:{schedule_id}"),
            clients=clients,
            machine=machine,
            clock_factory=clock_factory,
        )
    )


def run(schedule_id: int, revision: int | None = None) -> int:
    config = ConfigBoot()
    config.read_process_environment()
    machine = machine_from_boot(config)
    image = process_context.loaded_code_image()
    version = CodeVersion(image)
    gate = ProcessDbGate(process=process_name(), version=version.get)

    def database() -> Database:
        return Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host),
        )

    def clock_factory() -> Clock:
        return Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS)

    clients = process_clients(config=config, database=database, machine=machine)
    primary: BaseException | None = None
    try:
        return engine.run(
            schedule_id,
            revision,
            database=database,
            inputs=ScheduleInputs(database, clients.event_pipeline, image, machine=machine),
            bind_actor=partial(
                _bind_schedule_actor, clients=clients, clock_factory=clock_factory, machine=machine
            ),
            load_plugins=partial(
                meiva.ensure_plugins_loaded,
                config=config,
                clock_factory=clock_factory,
                producer=clients.event_pipeline,
            ),
        )
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            clients.close(pipeline_timeout=2)
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note(f"schedule clients cleanup failed: {cleanup!r}")


def main() -> None:
    if len(sys.argv) not in (2, 3):
        logger.error(
            "Usage: python -m services.wake.schedule_manager.runner <schedule_id> [revision]"
        )
        raise SystemExit(2)

    refusal = prod_service_checkout_error(Path(__file__).resolve().parents[3])
    if refusal is not None:
        logger.error("schedule runner refused: {}", refusal)
        raise SystemExit(3)
    revision = int(sys.argv[2]) if len(sys.argv) == 3 else None
    if revision is not None and revision < 0:
        raise SystemExit(2)
    raise SystemExit(run(int(sys.argv[1]), revision))


if __name__ == "__main__":
    main()
