"""Explicit configuration and machine inputs for one CLI test invocation."""

import pytest

from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from base.host.system.tests.fake_commands import fake_host
from base.sessions.tests.refuse_helper import refuse_helper
from cli.database.runtime import CommandRuntime

__all__ = ["command_config_machine", "command_machine", "command_runtime"]


@pytest.fixture
def command_runtime() -> CommandRuntime:
    """Own function-local config overlays and identity caches for this invocation.

    The host is inert: it finds no program and runs no command, so a test that reaches for the
    host must say what the host answers (`fake_host(...)`).
    """
    config = snapshot_process_config()
    return CommandRuntime(
        config=config,
        machine=machine_from_boot(config),
        helper=refuse_helper,
        host=fake_host(),
        timers=ManualTimers(),
    )


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
