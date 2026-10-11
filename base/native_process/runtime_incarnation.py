"""The admitted runtime's ownership token, never inferred from an agent id.

Admission must commit before binding a token; reading a replacement's token
from the DB on an exit path would defeat the fence.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class RuntimeIncarnation:
    agent_id: int
    generation: UUID
    owner: UUID

    def require_agent(self, agent_id: int) -> RuntimeIncarnation:
        """Validate the explicit original admission before using its authority."""
        if self.agent_id != agent_id:
            raise RuntimeError("runtime incarnation belongs to a different agent")
        return self
