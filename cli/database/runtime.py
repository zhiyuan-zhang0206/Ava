"""Configuration, machine identity, helper and host access owned by one operator invocation."""

from __future__ import annotations

from dataclasses import dataclass

from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.host.permissions_helper.client import HelperFactory
from base.host.system.hostport import Host
from base.sessions.helperproc import helper_client

__all__ = ["CommandRuntime"]


@dataclass(frozen=True)
class CommandRuntime:
    """Keep one lazy identity through dispatch and embedded lifecycle legs."""

    config: ConfigBoot
    machine: MachineIdentity
    helper: HelperFactory
    host: Host

    @classmethod
    def create(cls) -> CommandRuntime:
        """Construct without reading configuration, identity or network inputs.

        This is the one place the real permissions-helper factory and the real host are chosen:
        the helper only builds a client (and reads the helper's port) when a macOS code path asks
        for one, and the host only answers when a step asks it to find or run something."""
        config = ConfigBoot()
        return cls(
            config=config,
            machine=machine_from_boot(config),
            helper=helper_client,
            host=Host.system(),
        )
