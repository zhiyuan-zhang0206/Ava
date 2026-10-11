"""Gateway entry composition; its first-load image is an immutable code fact."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from base.agents.context.clients import ClientSet
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import ava_home
from base.telemetry import EventPipeline, build_pipeline

__all__ = ["LOADED_IMAGE", "GatewayProcess"]

# Both executable entries import this module before serving. Ordinary repeated
# imports share this frozen fact; a reload worker is a new process and captures
# its own loaded generation. Mutable admission and resources belong to each root.
LOADED_IMAGE = LoadedCommit.capture(Path(__file__).resolve().parents[2])


class GatewayProcess:
    """The configuration, admission and writer owned by one Gateway entry."""

    def __init__(
        self, *, config: ConfigBoot, image: LoadedCommit, machine: MachineIdentity
    ) -> None:
        self.config = config
        self.machine = machine
        self.image = image
        self.version = CodeVersion(image)
        self.gate = ProcessDbGate(version=self.version.get, process="gateway")
        self.clients = ClientSet(
            database=self.database,
            pipeline_factory=lambda: build_pipeline(
                database=self.database, machine=self.machine, home=ava_home().resolve()
            ),
        )

    def database(self) -> Database:
        """Read the current root config, retaining this entry's original gate."""
        return Database(
            db_config_from_boot(self.config), gate=self.gate, local_host=self.machine.host
        )

    def event_pipeline(self) -> EventPipeline:
        """Open the writer on first use; logging and ASGI reuse the same object."""
        return self.clients.event_pipeline()

    @contextmanager
    def lifetime(self) -> Generator[GatewayProcess]:
        """Close constructed clients even when startup or serving raises."""
        primary: BaseException | None = None
        try:
            yield self
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                self.clients.close()
            except BaseException as cleanup:
                if primary is None:
                    raise
                primary.add_note(f"Gateway writer cleanup also failed: {cleanup!r}")
