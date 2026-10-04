"""Pin the identity a test acts as: bind an `AvaContext` carrying it for the rest of the test.

`identity_restore` (autouse) puts the previous context back afterwards. Kept apart from that plugin
because tests and `env_bootstrap` import these helpers directly, and a pytest plugin module that is
imported before pytest registers it cannot be assertion-rewritten.
"""

from __future__ import annotations

from ava.sdk_surface import process_context
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity, ExternalLease


def pin_agent(
    agent_id: int | None,
    *,
    owns_loop: bool = True,
    actor: str | None = None,
    lease: ExternalLease | None = None,
) -> None:
    """Bind a context acting as `agent_id` for the rest of this test; the autouse fixture below
    puts the previous one back."""
    process_context.bind_process(
        AvaContext(
            identity=AgentIdentity(agent_id=agent_id, owns_loop=owns_loop, actor=actor, lease=lease)
        )
    )


def pin_no_identity() -> None:
    """Leave this test with no bound context (a process that is no agent)."""
    process_context.unbind_process()


def exec_context(agent_id: int | None, *, actor: str | None = None) -> AvaContext:
    """The host-side context of a turn that runs as `agent_id`: what an exec request carries."""
    return AvaContext(identity=AgentIdentity(agent_id=agent_id, owns_loop=True, actor=actor))
