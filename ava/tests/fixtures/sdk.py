"""Consumer-local SDK composition from the prepared process configuration.

Each test owns one lazy ClientSet, machine identity and clock factory. Guards receive
the SDK identity slot and recorder teardown through ordinary fixture overrides;
neither global guard needs to import the SDK to protect an unrelated collector.
"""

from collections.abc import Iterator
from types import ModuleType

import pytest

import ava
from ava.boot import process_clients
from ava.sdk_surface import metering, process_context
from ava.sdk_surface.installation import installed
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.clock import SYSTEM_TIMERS, Clock, clock_config_from_boot
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot

__all__ = ["sdk_environment", "sdk_identity", "sdk_metering"]


@pytest.fixture
def sdk_environment(
    process_config: ConfigBoot, machine_identity: MachineIdentity
) -> Iterator[AvaContext]:
    """Own one SDK root for this test; repinning borrows its clients and machine.

    The prepared configuration remains independent of the test's bare home.
    Identity and metering guards finish before these clients are closed.
    """
    try:
        previous = ava.context
    except AttributeError:
        previous = None
    context = AvaContext(
        identity=AgentIdentity(agent_id=1, owns_loop=True),
        clients=process_clients(config=process_config, machine=machine_identity),
        machine=machine_identity,
        clock_factory=lambda: Clock(clock_config_from_boot(process_config), timers=SYSTEM_TIMERS),
    )
    process_context.bind_context(context)
    try:
        yield context
    finally:
        try:
            context.clients.close()
        finally:
            if previous is None:
                del ava.context
            else:
                process_context.bind_context(previous)


@pytest.fixture
def sdk_identity(sdk_environment: AvaContext) -> ModuleType:
    """Supply the real context slot to the global identity restoration window."""
    return ava


@pytest.fixture
def sdk_metering(sdk_environment: AvaContext) -> Iterator[None]:
    """Remove only the installation's recorded outermost usage recorders."""
    yield
    current = installed()
    if current is not None and current.metered:
        metering.uninstall(current.metered)
