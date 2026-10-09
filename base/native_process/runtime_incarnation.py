"""The admitted runtime's ownership token, never inferred from an agent id.

Protocol zero means no identity-envelope capability has been proven. Admission
must commit before binding a token; reading a replacement's token from the DB
on an exit path would defeat the fence.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

# The wire value advertising the identity-envelope protocol on agents_meta
# (task #4122): the caller gate compares callers against it; hosted admission
# writes zero. Deliberately not a config field -- changing it is a
# protocol-version bump (writer + gate move together in code), not a
# per-cluster behavioral knob.
RUNTIME_PROTOCOL_V1 = 1


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
