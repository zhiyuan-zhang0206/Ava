"""SDK composition and CLI entrypoint for an independent schedule process."""

from __future__ import annotations

import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path

from loguru import logger

import ava
from ava.sdk_surface import process_context
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.clock import Clock, clock_config_from_boot
from base.config import ConfigBoot
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.native_process.code_version import CodeVersion
from base.paths import prod_service_checkout_error
from base.telemetry import process_name
from gateway.schedules import runner as engine


def _bind_schedule_actor(
    schedule_id: int, *, clients: ClientSet, clock_factory: Callable[[], Clock]
) -> None:
    """Attribute SDK operations to this schedule using the process's client owner."""
    ava.bind_context(
        AvaContext(
            identity=AgentIdentity(agent_id=None, owns_loop=True, actor=f"schedule:{schedule_id}"),
            clients=clients,
            clock_factory=clock_factory,
        )
    )


def run(schedule_id: int, revision: int | None = None) -> int:
    """Run a schedule with the SDK policy inputs owned by this process."""
    config = ConfigBoot()
    config.read_process_environment()
    image = ava.loaded_code_image()
    version = CodeVersion(image)
    gate = ProcessDbGate(process=process_name(), version=version.get)

    def database() -> Database:
        return Database(
            db_config_from_boot(config),
            gate=gate,
            local_host=lambda: config.view.general.machine_host.strip() or "localhost",
        )

    def clock_factory() -> Clock:
        return Clock(clock_config_from_boot(config))

    clients = process_context.process_clients(config=config, database=database)
    primary: BaseException | None = None
    try:
        return engine.run(
            schedule_id,
            revision,
            database=database,
            inputs=ScheduleInputs(database, clients.event_pipeline, image),
            bind_actor=partial(_bind_schedule_actor, clients=clients, clock_factory=clock_factory),
            load_plugins=partial(
                ava.ensure_plugins_loaded,
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
    # The runner's checkout anchors every subprocess it spawns. Refuse a dev
    # checkout against the production home before reading or executing a script.
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
