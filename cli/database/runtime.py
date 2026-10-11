"""Configuration and machine identity owned by one operator invocation."""

from __future__ import annotations

from dataclasses import dataclass

from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot

__all__ = ["CommandRuntime"]


@dataclass(frozen=True)
class CommandRuntime:
    """Keep one lazy identity through dispatch and embedded lifecycle legs."""

    config: ConfigBoot
    machine: MachineIdentity

    @classmethod
    def create(cls) -> CommandRuntime:
        """Construct without reading configuration, identity or network inputs."""
        config = ConfigBoot()
        return cls(config=config, machine=machine_from_boot(config))
