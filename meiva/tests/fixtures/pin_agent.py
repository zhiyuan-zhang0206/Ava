# meiva:link meiva/tests/fixtures/docs/sdk-composition.meiva.okf.md

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable

import pytest

import meiva
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity, ExternalLease
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config.tests.configuration import snapshot_process_config
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedServiceResources, HostedTurnResources
from meiva.boot import process_clients


@pytest.fixture
async def hosted_resources() -> AsyncGenerator[HostedTurnResources]:
    """An explicit test service owns watcher/completion results until fixture join."""
    service = HostedServiceResources()
    try:
        yield await service.turn()
    finally:
        await service.aclose()


def pin_agent(
    agent_id: int | None,
    *,
    owns_loop: bool = True,
    actor: str | None = None,
    lease: ExternalLease | None = None,
    incarnation: RuntimeIncarnation | None = None,
    clock_factory: Callable[[], Clock] | None = None,
    clients: ClientSet | None = None,
    machine: MachineIdentity | None = None,
) -> None:
    bound = getattr(meiva, "context", None)
    meiva.context = MeivaContext(
        identity=AgentIdentity(agent_id=agent_id, owns_loop=owns_loop, actor=actor, lease=lease),
        original_incarnation=incarnation,
        machine=machine if machine is not None else (None if bound is None else bound.machine),
        clients=clients if clients is not None else (bound.clients if bound else process_clients()),
        clock_factory=(
            clock_factory
            if clock_factory is not None
            else (None if bound is None else bound.clock_factory)
        ),
    )


def pin_no_identity() -> None:
    del meiva.context


def exec_context(
    agent_id: int | None,
    *,
    actor: str | None = None,
    incarnation: RuntimeIncarnation | None = None,
    resources: HostedTurnResources | None = None,
) -> MeivaContext:
    config = snapshot_process_config()
    machine = machine_from_boot(config)
    return MeivaContext(
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True, actor=actor),
        original_incarnation=incarnation,
        machine=machine,
        hosted_resources=resources,
        clients=process_clients(config=config, machine=machine),
        clock_factory=lambda: Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
    )
