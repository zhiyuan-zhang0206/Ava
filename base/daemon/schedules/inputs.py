"""Value contract for resources borrowed by an in-process schedule script."""

from collections.abc import Callable
from dataclasses import dataclass

from base.db import Database
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import EventPipeline

__all__ = ["ScheduleInputs"]


@dataclass(frozen=True)
class ScheduleInputs:
    """Use the executing entry's builders and immutable loaded-code fact."""

    database: Callable[[], Database]
    producer: Callable[[], EventPipeline]
    image: LoadedCommit
