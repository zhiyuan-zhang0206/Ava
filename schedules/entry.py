"""Explicit inputs shared by runner-hosted and directly executed schedule entries."""

from collections.abc import Generator
from contextlib import contextmanager
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.clock import Clock, clock_config_from_boot
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.paths.home import resolve_ava_home
from base.native_process.code_version import CodeVersion
from base.telemetry import build_pipeline, process_name
from base.daemon.schedules.inputs import ScheduleInputs

__all__ = ["fresh_schedule_entry", "schedule_entry"]


@contextmanager
def schedule_entry(inputs: ScheduleInputs) -> Generator[ScheduleInputs]:
    """Borrow explicit runner inputs without touching the shared SDK module."""
    yield inputs


@contextmanager
def fresh_schedule_entry() -> Generator[ScheduleInputs]:
    """Own this fresh executable's SDK context and release its clients on exit."""
    import ava

    image = ava.loaded_code_image()
    config = ConfigBoot()
    config.boot()
    machine = machine_from_boot(config)
    home = resolve_ava_home()
    version = CodeVersion(image)
    gate = ProcessDbGate(process=process_name(), version=version.get)

    def database() -> Database:
        return Database(
            db_config_from_boot(config), gate=gate, direct_url=ClusterDirectUrl(machine.host)
        )

    clients = ClientSet(
        database=database,
        pipeline_factory=lambda: build_pipeline(database=database, machine=machine, home=home),
    )
    prior = getattr(ava, "context", None)
    ava.bind_context(
        AvaContext(
            clients=clients,
            machine=machine,
            clock_factory=lambda: Clock(clock_config_from_boot(config)),
        )
    )
    try:
        yield ScheduleInputs(database, clients.event_pipeline, image, machine=machine)
    except BaseException as primary:
        try:
            clients.close(pipeline_timeout=2)
        except BaseException as cleanup:
            primary.add_note(f"schedule entry cleanup failed: {cleanup!r}")
        raise
    else:
        clients.close(pipeline_timeout=2)
    finally:
        if prior is None:
            ava.unbind_context()
        else:
            ava.bind_context(prior)
