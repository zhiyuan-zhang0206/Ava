"""The admitted runtime's ownership token, never inferred from an agent id.

Protocol zero means no identity-envelope capability has been proven. Admission
must commit before binding a token; reading a replacement's token from the DB
on an exit path would defeat the fence.
"""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class RuntimeIncarnation:
    agent_id: int
    generation: UUID
    owner: UUID


_child_incarnation: RuntimeIncarnation | None = None


def bind_child_incarnation(incarnation: RuntimeIncarnation) -> None:
    """Bind only the original host incarnation carried in an execution request."""
    global _child_incarnation  # noqa: PLW0603 — one request per execution child
    _child_incarnation = incarnation


def current_incarnation(agent_id: int) -> RuntimeIncarnation | None:
    from shared.turn_identity import current_turn_incarnation

    incarnation = current_turn_incarnation() or _child_incarnation
    if incarnation is not None and incarnation.agent_id != agent_id:
        raise RuntimeError("runtime incarnation belongs to a different agent")
    return incarnation
