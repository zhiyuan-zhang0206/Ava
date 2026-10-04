"""What the native runtime remembers about each agent's bound impersonation relay.

The agent host builds one `RelaySupervision` and holds it: it hands it to the turns it runs
(`AvaContext.relays`, read by the claim gate) and to its own supervision and activation calls
(`agent.impersonation`). The agent-host service drives several agents in one process, so every
record is keyed by agent; entries die with the process or are popped when the native agent
regains control.
"""

from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime


class RelayChild:
    """The native runtime's bound relay for one lease, spawned at activation."""

    __slots__ = ("lease_id", "process", "spawned_at", "token")

    def __init__(
        self,
        lease_id: str,
        process: subprocess.Popen[bytes],
        token: str,
        spawned_at: float,
    ) -> None:
        self.lease_id = lease_id
        self.process = process
        self.token = token
        self.spawned_at = spawned_at


class RelaySupervision:
    """The relays this process spawned, the anchor verdicts pending, and this process's start.

    - `children`: agent -> its bound relay.
    - `anchor_obscured`: agents whose current lease saw one not-alive pass with unreadable
      (denied/unknown) anchors, so the second consecutive pass aborts. Keyed to the lease id it
      was recorded for, so a successor lease never inherits a verdict.
    - `started_monotonic` / `started_wall`: this process's start (task #3998 fresh-start
      plumbing). Monotonic for the window age; wall-clock for comparing DB heartbeat timestamps
      against this process's own boot.
    """

    def __init__(self) -> None:
        self.children: dict[int, RelayChild] = {}
        self.anchor_obscured: dict[int, str] = {}
        self.started_monotonic = time.monotonic()
        self.started_wall = datetime.now(UTC)

    def drop(self, agent_id: int) -> None:
        """Forget supervision state for a departed agent.

        The relay process itself is not killed here — it self-exits as soon as the lease
        reaches a terminal status (the terminate trigger revokes it), and a relay that keeps
        running until then is still delivering real messages.
        """
        self.children.pop(agent_id, None)
        self.anchor_obscured.pop(agent_id, None)
