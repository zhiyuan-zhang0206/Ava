"""Pin the identity a test acts as: bind an `AvaContext` carrying it for the rest of the test.

`identity_restore` (autouse) puts the previous context back afterwards. Kept apart from that plugin
because tests and `env_bootstrap` import these helpers directly, and a pytest plugin module that is
imported before pytest registers it cannot be assertion-rewritten.
"""

from __future__ import annotations

import ava
from ava.sdk_surface.process_context import process_clients
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity, ExternalLease
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources


def pin_agent(
    agent_id: int | None,
    *,
    owns_loop: bool = True,
    actor: str | None = None,
    lease: ExternalLease | None = None,
    incarnation: RuntimeIncarnation | None = None,
) -> None:
    """Bind a context acting as `agent_id` for the rest of this test; the autouse fixture below
    puts the previous one back."""
    bound = getattr(ava, "context", None)
    ava.context = AvaContext(
        identity=AgentIdentity(agent_id=agent_id, owns_loop=owns_loop, actor=actor, lease=lease),
        original_incarnation=incarnation,
        # The identity changes, the connections stay: a test's `use_client` or fake SQL slot
        # entered before it pins an agent keeps applying.
        clients=bound.clients if bound else process_clients(),
    )


def pin_no_identity() -> None:
    """Leave this test with no bound context (a process that is no agent)."""
    del ava.context


def exec_context(
    agent_id: int | None,
    *,
    actor: str | None = None,
    incarnation: RuntimeIncarnation | None = None,
    resources: HostedTurnResources | None = None,
) -> AvaContext:
    """The host-side context of a turn that runs as `agent_id`: what an exec request carries."""
    return AvaContext(
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True, actor=actor),
        original_incarnation=incarnation,
        hosted_resources=resources,
        clients=process_clients(),
    )
