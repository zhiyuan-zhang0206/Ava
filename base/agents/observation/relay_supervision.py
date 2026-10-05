"""What the native runtime remembers about each agent's bound impersonation relay.

The agent host builds one `RelaySupervision` and holds it: it hands it to the turns it runs
(`AvaContext.relays`, read by the claim gate) and to its own supervision and activation calls
(`agent.impersonation`). The agent-host service drives several agents in one process, so every
record is keyed by agent; entries die with the process or are popped when the native agent
regains control.
"""

from __future__ import annotations

import subprocess


class RelayChild:
    """The native runtime's bound relay for one lease, spawned at activation."""

    __slots__ = ("generation", "lease_id", "process", "spawned_at", "token")

    def __init__(
        self,
        lease_id: str,
        process: subprocess.Popen[bytes],
        token: str,
        spawned_at: float,
        generation: int = 0,
    ) -> None:
        self.generation = generation
        self.lease_id = lease_id
        self.process = process
        self.token = token
        self.spawned_at = spawned_at


class RelaySupervision:
    """The known child handles, fenced by lease and transport generation."""

    def __init__(self) -> None:
        self.children: dict[int, RelayChild] = {}

    def drop(self, agent_id: int) -> None:
        """Forget the handle; a terminal lease revokes the child independently."""
        self.children.pop(agent_id, None)
