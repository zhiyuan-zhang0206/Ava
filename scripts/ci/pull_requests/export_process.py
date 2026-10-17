"""The standalone CI samplers' database gate and finite event-writer lifetime."""

from collections.abc import Generator
from contextlib import contextmanager

from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.log import logger
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
from base.telemetry import DrainStatus, EventPipeline, build_pipeline

__all__ = ["owned_event_pipeline"]


@contextmanager
def owned_event_pipeline(process: str, *, image: LoadedCommit | None) -> Generator[EventPipeline]:
    """Own the sampler's actual writer; a scheduled caller supplies its existing producer."""
    if image is None:
        raise ValueError("a standalone exporter requires its entry's loaded image")
    version = CodeVersion(image)
    gate = ProcessDbGate(process=process, version=version.get)

    def database() -> Database:
        return Database.from_settings(
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host),
        )

    config = ConfigBoot()
    machine = machine_from_boot(config)
    pipeline = build_pipeline(database=database, machine=machine, home=meiva_home())
    primary: BaseException | None = None
    try:
        yield pipeline
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            receipt = pipeline.stop(timeout=2)
            if receipt.status is DrainStatus.UNFINISHED:
                logger.error("{} event writer remains unfinished after its drain budget", process)
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note(f"exporter event writer cleanup failed: {cleanup!r}")
