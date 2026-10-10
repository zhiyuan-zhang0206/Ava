"""Consumer-local SDK composition from the prepared process configuration.

The session owns one lazy ClientSet and clock factory. Function guards receive
the SDK identity slot and recorder teardown through ordinary fixture overrides;
neither global guard needs to import the SDK to protect an unrelated collector.
"""

from collections.abc import Iterator
from typing import cast

import pytest

import ava
from ava.sdk_surface import install, metering
from ava.sdk_surface.process_context import process_clients
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.clock import Clock, clock_config_from_boot
from base.config import ConfigBoot
from tests.fixtures.identity_restore import SdkIdentitySlot

__all__ = ["sdk_environment", "sdk_identity", "sdk_metering"]

_SDK_CONTEXT_KEY = pytest.StashKey[AvaContext]()


@pytest.fixture(scope="session")
def sdk_environment(process_config: ConfigBoot, pytestconfig: pytest.Config) -> AvaContext:
    """Bind the prepared session context for consumer identity and recorder guards.

    Consumer owners bind this capability with ``sdk_identity`` and
    ``sdk_metering``. All bindings share one owned ClientSet and clock factory;
    repinning borrows them until pytest closes the session owner.
    """
    if _SDK_CONTEXT_KEY not in pytestconfig.stash:
        context = AvaContext(
            identity=AgentIdentity(agent_id=1, owns_loop=True),
            clients=process_clients(config=process_config),
            clock_factory=lambda: Clock(clock_config_from_boot(process_config)),
        )
        pytestconfig.stash[_SDK_CONTEXT_KEY] = context
        pytestconfig.add_cleanup(context.clients.close)
    context = pytestconfig.stash[_SDK_CONTEXT_KEY]
    ava.bind_context(context)
    return context


@pytest.fixture
def sdk_identity(sdk_environment: AvaContext) -> SdkIdentitySlot:
    """Supply the real context slot to the global identity restoration window."""
    return cast(SdkIdentitySlot, ava)


@pytest.fixture
def sdk_metering(sdk_environment: AvaContext) -> Iterator[None]:
    """Remove only the installation's recorded outermost usage recorders."""
    yield
    current = install.installed()
    if current is not None and current.metered:
        metering.uninstall(current.metered)
