# meiva:link meiva/tests/fixtures/docs/sdk-composition.meiva.okf.md

from collections.abc import Iterator
from types import ModuleType

import pytest

import meiva
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot
from meiva.boot import process_clients
from meiva.sdk_surface import metering, process_context
from meiva.sdk_surface.installation import installed

__all__ = ["sdk_environment", "sdk_identity", "sdk_metering"]


@pytest.fixture
def sdk_environment(
    process_config: ConfigBoot, machine_identity: MachineIdentity
) -> Iterator[MeivaContext]:
    """Own one SDK root for this test; repinning borrows its clients and machine.

    The prepared configuration remains independent of the test's bare home.
    Identity and metering guards finish before these clients are closed.
    """
    try:
        previous = meiva.context
    except AttributeError:
        previous = None
    context = MeivaContext(
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
                del meiva.context
            else:
                process_context.bind_context(previous)


@pytest.fixture
def sdk_identity(sdk_environment: MeivaContext) -> ModuleType:
    """Supply the real context slot to the global identity restoration window."""
    return meiva


@pytest.fixture
def sdk_metering(sdk_environment: MeivaContext) -> Iterator[None]:
    """Remove only the installation's recorded outermost usage recorders."""
    yield
    current = installed()
    if current is not None and current.metered:
        metering.uninstall(current.metered)
