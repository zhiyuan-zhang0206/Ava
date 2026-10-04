"""Who a process acts as — the identity part of `AvaContext`.

`AgentIdentity` is a frozen value. The agent host builds one for a turn it serves, the exec child
and a script an agent launched build theirs from a description, and an external controller derives
one carrying its lease. Nothing here is process state: the identity of a process is whatever
`AvaContext` it is bound to (`ava.sdk_surface.process_context`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExternalLease:
    """An agent identity borrowed by an external controller.

    `validate` rechecks the lease at every SDK read and returns the borrowed agent id; it raises
    once the lease no longer permits the caller. `config` is the borrowed agent's pins and
    plugin-config view, or None until the attachment has loaded them.
    """

    agent_id: int
    validate: Callable[[], int]
    config: Callable[[], tuple[Mapping[str, Any], Any] | None]


@dataclass(frozen=True)
class AgentIdentity:
    """The agent a process acts as, and what it may do as that agent.

    - `agent_id`: the agent whose work this process attributes its calls to; None for a process
      with no agent (a gateway-hosted schedule). A real id is always >= 1, so None, not 0, is
      "no agent": a premature write then hits a NOT NULL column instead of landing a ghost row.
    - `owns_loop`: True where this process is the agent's own turn path (the exec child, a host
      turn); False in a script the agent launched, which must not compact or restart the agent
      whose identity it carries.
    - `actor`: the provenance principal of a process that acts as something other than an agent
      (`schedule:7`); None derives provenance from the agent id.
    - `lease`: set only while an external controller is attached.
    """

    agent_id: int | None
    owns_loop: bool
    actor: str | None = None
    lease: ExternalLease | None = None

    def describe(self) -> dict[str, Any]:
        """The JSON form a request envelope carries; a lease is a live object and has none."""
        if self.lease is not None:
            raise ValueError("an identity borrowed through an external lease cannot be described")
        return {"agent_id": self.agent_id, "owns_loop": self.owns_loop, "actor": self.actor}

    @classmethod
    def from_description(cls, description: Mapping[str, Any]) -> AgentIdentity:
        agent_id = description["agent_id"]
        actor = description["actor"]
        return cls(
            agent_id=None if agent_id is None else int(agent_id),
            owns_loop=bool(description["owns_loop"]),
            actor=None if actor is None else str(actor),
        )
