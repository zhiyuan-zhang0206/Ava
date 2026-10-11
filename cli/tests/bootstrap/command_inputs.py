"""Explicit configuration and machine inputs for one CLI test invocation."""

import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from cli.database.runtime import CommandRuntime
from tests.fixtures.configuration import snapshot_process_config

__all__ = ["command_config_machine", "command_machine", "command_runtime"]


@pytest.fixture
def command_runtime() -> CommandRuntime:
    """Own function-local config overlays and identity caches for this invocation."""
    config = snapshot_process_config()
    return CommandRuntime(config=config, machine=machine_from_boot(config))


@pytest.fixture
def command_machine(command_runtime: CommandRuntime) -> MachineIdentity:
    """Borrow only the machine capability from this invocation's existing owner."""
    return command_runtime.machine


@pytest.fixture
def command_config_machine(
    command_runtime: CommandRuntime,
) -> tuple[ConfigBoot, MachineIdentity]:
    """Borrow both capabilities together from the same invocation owner."""
    return command_runtime.config, command_runtime.machine
