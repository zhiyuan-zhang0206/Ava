"""Explicit inputs shared by runner-hosted and directly executed schedule entries."""

from collections.abc import Generator
from contextlib import contextmanager
from functools import partial

from base.agents.context.clients import ClientSet
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process.code_version import CodeVersion
from base.telemetry import build_pipeline, process_name
from base.daemon.schedules.inputs import ScheduleInputs

__all__ = ["schedule_entry"]


@contextmanager
def schedule_entry(inputs: ScheduleInputs | None) -> Generator[ScheduleInputs]:
    """Borrow runner inputs, or own resources at the direct Python entry only."""
    if inputs is not None:
        yield inputs
        return
    import ava

    image = ava.loaded_code_image()
    version = CodeVersion(image)
    gate = ProcessDbGate(process=process_name(), version=version.get)
    database = partial(Database.from_settings, gate=gate)
    clients = ClientSet(
        database=database, pipeline_factory=lambda: build_pipeline(database=database)
    )
    try:
        yield ScheduleInputs(database, clients.event_pipeline, image)
    except BaseException as primary:
        try:
            clients.close(pipeline_timeout=2)
        except BaseException as cleanup:
            primary.add_note(f"schedule entry cleanup failed: {cleanup!r}")
        raise
    else:
        clients.close(pipeline_timeout=2)
