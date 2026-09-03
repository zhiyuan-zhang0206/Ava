"""The admitted runtime's ownership token, never inferred from an agent id.

Protocol zero means no identity-envelope capability has been proven. Admission
must commit before binding a token; reading a replacement's token from the DB
on an exit path would defeat the fence.
"""

from dataclasses import dataclass
from uuid import UUID, uuid4


@dataclass(frozen=True)
class RuntimeIncarnation:
    agent_id: int
    generation: UUID
    owner: UUID


_boot_owner = uuid4()
_current: RuntimeIncarnation | None = None


def new_process_incarnation(agent_id: int) -> RuntimeIncarnation:
    return RuntimeIncarnation(agent_id, uuid4(), _boot_owner)


def bind_process_incarnation(incarnation: RuntimeIncarnation) -> None:
    """Bind after successful process admission, before creating runtime tasks."""
    global _current  # noqa: PLW0603 — one admitted agent per process
    _current = incarnation


def current_incarnation(agent_id: int) -> RuntimeIncarnation | None:
    incarnation = _current
    if incarnation is not None and incarnation.agent_id != agent_id:
        raise RuntimeError("runtime incarnation belongs to a different agent")
    return incarnation
