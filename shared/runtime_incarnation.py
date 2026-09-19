"""The admitted runtime's ownership token, never inferred from an agent id.

Protocol zero means no identity-envelope capability has been proven. Admission
must commit before binding a token; reading a replacement's token from the DB
on an exit path would defeat the fence.
"""

from dataclasses import dataclass
from uuid import UUID

# The wire value advertising the identity-envelope protocol on agents_meta
# (task #4122): admission writes it under a current managed publication, and
# the caller gate compares callers against it. Deliberately not a config field
# -- changing it is a protocol-version bump (writer + gate move together in
# code), not a per-cluster behavioral knob.
RUNTIME_PROTOCOL_V1 = 1


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
