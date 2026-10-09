"""Pin the identity a test acts as: bind an `AvaContext` carrying it for the rest of the test.

`identity_restore` (autouse) puts the previous context back afterwards. Kept apart from that plugin
because tests and `env_bootstrap` import these helpers directly, and a pytest plugin module that is
imported before pytest registers it cannot be assertion-rewritten.
"""

from __future__ import annotations

import ava
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity, ExternalLease
from base.db import Database


def pin_agent(
    agent_id: int | None,
    *,
    owns_loop: bool = True,
    actor: str | None = None,
    lease: ExternalLease | None = None,
) -> None:
    """Bind a context acting as `agent_id` for the rest of this test; the autouse fixture below
    puts the previous one back."""
    bound = getattr(ava, "context", None)
    ava.context = AvaContext(
        identity=AgentIdentity(agent_id=agent_id, owns_loop=owns_loop, actor=actor, lease=lease),
        # The identity changes, the connections stay: a test's `use_client` or fake SQL slot
        # entered before it pins an agent keeps applying.
        clients=bound.clients if bound else ClientSet(database=Database.from_settings),
    )


def pin_no_identity() -> None:
    """Leave this test with no bound context (a process that is no agent)."""
    del ava.context


def exec_context(agent_id: int | None, *, actor: str | None = None) -> AvaContext:
    """The host-side context of a turn that runs as `agent_id`: what an exec request carries."""
    return AvaContext(identity=AgentIdentity(agent_id=agent_id, owns_loop=True, actor=actor))
