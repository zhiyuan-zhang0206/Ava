"""Original native admission and resource scopes awaiting explicit-owner migration.

These scopes fence lifecycle and resource settlement. They do not provide SDK
identity, ordinary log attribution or a current agent. Graph identity belongs to
Runtime[AvaContext]; process SDK identity belongs to the execution entry.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Generator
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from base.native_process.runtime_incarnation import RuntimeIncarnation


@dataclass(frozen=True)
class _TurnExecutionIdentity:
    incarnation: RuntimeIncarnation | None
    native_work_id: UUID | None = None


_TURN_INCARNATION: ContextVar[_TurnExecutionIdentity | None] = ContextVar(
    "ava_turn_incarnation", default=None
)


@contextlib.contextmanager
def bind_native_work(work_id: UUID | None) -> Generator[None, None, None]:
    """Carry one invocation in the existing turn identity through DB recovery."""
    previous = _TURN_INCARNATION.get()
    identity = _TurnExecutionIdentity(None if previous is None else previous.incarnation, work_id)
    token = _TURN_INCARNATION.set(identity)
    try:
        yield
    finally:
        _TURN_INCARNATION.reset(token)


def current_native_work_id() -> UUID | None:
    identity = _TURN_INCARNATION.get()
    return None if identity is None else identity.native_work_id


@dataclass
class HostedTurnResources:
    """Actual unresolved domains held by one turn Task, never by the model cache."""

    unresolved: dict[Path, object | None] = field(default_factory=dict[Path, object | None])
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    completions: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])

    def complete(self, request: Path, expected: object | None) -> bool:
        """Only the original resource owner may discharge its exact entry."""
        if request not in self.unresolved or self.unresolved[request] is not expected:
            return False
        del self.unresolved[request]
        self.changed.set()
        return True


_TURN_RESOURCES: ContextVar[HostedTurnResources | None] = ContextVar(
    "ava_turn_resources", default=None
)


@contextlib.contextmanager
def bind_hosted_resources(scope: HostedTurnResources) -> Generator[None, None, None]:
    """Share actual resource ownership across this turn's copied node contexts."""
    token = _TURN_RESOURCES.set(scope)
    try:
        yield
    finally:
        _TURN_RESOURCES.reset(token)


def current_hosted_resources() -> HostedTurnResources | None:
    return _TURN_RESOURCES.get()


def hosted_resources_settled() -> bool:
    scope = _TURN_RESOURCES.get()
    return scope is None or not scope.unresolved


@contextlib.contextmanager
def bind_turn_identity(
    agent_id: int,
    *,
    incarnation: RuntimeIncarnation | None = None,
) -> Generator[None, None, None]:
    """Carry the original admission for native ownership, never SDK/log identity.

    The caller names the agent explicitly; an incarnation from another agent
    is invalid. Copied tasks and nested reset retain the existing native fence.
    """
    if incarnation is not None and incarnation.agent_id != agent_id:
        raise ValueError("turn incarnation belongs to a different agent")
    runtime_token = _TURN_INCARNATION.set(_TurnExecutionIdentity(incarnation))
    try:
        yield
    finally:
        _TURN_INCARNATION.reset(runtime_token)


def current_turn_incarnation() -> RuntimeIncarnation | None:
    identity = _TURN_INCARNATION.get()
    return None if identity is None else identity.incarnation
