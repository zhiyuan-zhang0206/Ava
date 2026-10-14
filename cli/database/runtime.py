"""Configuration, machine identity and helper access owned by one operator invocation."""

from __future__ import annotations

from dataclasses import dataclass

from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.host.permissions_helper.client import HelperFactory
from base.sessions.helperproc import helper_client

__all__ = ["CommandRuntime"]


@dataclass(frozen=True)
class CommandRuntime:
    """Keep one lazy identity through dispatch and embedded lifecycle legs."""

    config: ConfigBoot
    machine: MachineIdentity
    helper: HelperFactory

    @classmethod
    def create(cls) -> CommandRuntime:
        """Construct without reading configuration, identity or network inputs.

        This is the one place the real permissions-helper factory is chosen: it only builds a
        client (and reads the helper's port) when a macOS code path asks for one."""
        config = ConfigBoot()
        return cls(config=config, machine=machine_from_boot(config), helper=helper_client)
