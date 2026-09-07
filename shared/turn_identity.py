"""Turn-scoped agent identity — the contextvar half of "who am I".

Phase 1 of `future/infra/agent-runner-as-server.md`: in the hosted runner many
agents' turns share one process, so the two process-level identity channels —
`ava._boot._agent_id` (set once by the agent bootstrap) and the `AVA_AGENT_ID`
environment variable (set once for launched children) — cannot distinguish
which agent's turn is executing. This module holds the third, innermost
channel: a contextvar the hosted dispatcher binds before creating an agent's
turn task. `asyncio.create_task` copies the creating context, and LangGraph
node tasks copy the loop-level context, so one bind at task creation covers
every node of the turn; the exec child re-establishes its identity from its
own boot (agent id carried in the request env) — it cannot inherit the
parent's contextvar — so agent code in the child sees the same identity.

Resolution order everywhere identity is read:

    turn contextvar  >  process bootstrap slot (`ava._boot`)  >  AVA_AGENT_ID env

Process mode binds nothing here, the contextvar stays None, and every read
falls through to the process slot / env — behavior unchanged.

This lives in `shared/` (not `ava/`) because identity consumers exist below
the `ava` layer (`shared/lm/_providers.py` cache affinity, `shared/resilience.py`
retry de-phasing) and the import layering is `shared < ava`. `ava._boot`
layers its process slot on top of this module's read.

`TurnScopedAgentId` at the bottom is the same resolution deferred to *render*
time, for sinks that are handed a value once and format it per record —
`shared/log.py` binds one into loguru's `extra`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Generator
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from shared.runtime_incarnation import RuntimeIncarnation

_TURN_AGENT_ID: ContextVar[int | None] = ContextVar("ava_turn_agent_id", default=None)
_TURN_INCARNATION: ContextVar[RuntimeIncarnation | None] = ContextVar(
    "ava_turn_incarnation", default=None
)


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
    """Bind `agent_id` as the current context's turn identity.

    The hosted dispatcher wraps turn-task creation in this (together with
    `shared.config.bind_agent_config`): bind -> create the task (which copies
    the context) -> reset. Nesting rebinds cleanly.
    """
    token = _TURN_AGENT_ID.set(agent_id)
    runtime_token = _TURN_INCARNATION.set(incarnation)
    try:
        yield
    finally:
        _TURN_INCARNATION.reset(runtime_token)
        _TURN_AGENT_ID.reset(token)


def current_turn_incarnation() -> RuntimeIncarnation | None:
    return _TURN_INCARNATION.get()


def current_turn_agent_id() -> int | None:
    """The turn identity bound in the current context, or None outside an agent turn."""
    return _TURN_AGENT_ID.get()


def effective_agent_id() -> int | None:
    """Turn identity if bound, else the ambient `AVA_AGENT_ID` env identity.

    The read for `shared/`-layer consumers (LLM cache affinity, retry
    de-phasing) that previously read the env var directly: in the hosted
    runner the env var is one value for the whole process, so the turn
    contextvar must win. Code above the `ava` layer should prefer
    `ava._boot.agent_id()`, which also consults the process bootstrap slot.
    """
    bound = _TURN_AGENT_ID.get()
    if bound is not None:
        return bound
    raw = os.environ.get("AVA_AGENT_ID")  # env-ok: process identity channel, not Settings config
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            return None
    return None


# ── Deferred attribution (log sinks) ──

# The agent a process attributes its records to when no turn is bound. Set by
# `shared.log.init_agent_process`; stays None in a process that hosts many
# agents' turns, and in one that owns no agent at all (gateway, ops).
_process_agent_id: int | None = None


def set_process_agent_id(agent_id: int | None) -> None:
    """Declare which agent this process is, for records written outside a turn.

    Deliberately NOT folded into `effective_agent_id`: that read answers "who is
    executing", and its env fallback is the launched-child channel. This slot
    answers "whose log file is this", which only the logging init knows.
    """
    global _process_agent_id  # noqa: PLW0603 — process-level singleton
    _process_agent_id = agent_id


class TurnScopedAgentId:
    """Deferred `agent_id` for a sink: resolves per record, not per process.

    `logger.configure(extra=...)` freezes its values at bind time, which is
    exactly right for one agent per process and wrong for the hosted runner,
    where one process writes log lines on behalf of every local agent. Binding
    an instance of this instead defers the answer to write time:

        turn contextvar  >  this process's agent  >  `-` (no agent)

    A caller that passes `agent_id=N` explicitly replaces the whole extra value
    and never reaches this — explicit attribution still wins.

    It renders as the resolved id everywhere a record is formatted:
    `__format__` for the human stderr format's `{extra[agent_id]:>3}`,
    `__str__` for the JSONL file sink (loguru serializes with `default=str`).
    Both run in the writing thread's context, so the turn binding is still live.
    """

    __slots__ = ()

    def resolve(self) -> str:
        """The agent id for the record being written, or the `-` no-agent sentinel."""
        bound = _TURN_AGENT_ID.get()
        if bound is not None:
            return str(bound)
        if _process_agent_id is not None:
            return str(_process_agent_id)
        return "-"

    def __str__(self) -> str:
        return self.resolve()

    def __format__(self, spec: str) -> str:
        return format(self.resolve(), spec)

    def __repr__(self) -> str:
        return f"TurnScopedAgentId({self.resolve()})"


TURN_SCOPED_AGENT_ID = TurnScopedAgentId()
