"""Construct process-local SDK contexts at their execution entry points.

The execution child, schedule runner and external attachment publish their context
on the local ``ava`` module. Hosted graph runs receive it through LangGraph Runtime
and never publish an agent identity on that shared module. This module owns no
current context, thread patch or live clients.
"""

from __future__ import annotations

import os
from typing import Any

from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.native_process.turn_identity import current_turn_agent_id


class ContextOutsideProcessError(AttributeError):
    """The SDK's local context was read outside an execution process."""


def process_clients(*, gateway_url: str | None = None) -> ClientSet:
    """Build a process's lazy clients from its own configuration."""
    from ava.sdk_surface import settings

    return ClientSet(gateway_url=gateway_url, database=settings.database)


def context_from_description(description: dict[str, Any]) -> AvaContext:
    """Rebuild an execution context without copying its host's live clients."""
    from ava.sdk_surface import settings

    return AvaContext.from_description(description, database=settings.database)


def launched_context() -> AvaContext | None:
    """Construct a launched script's context from its explicit environment channel.

    A native host turn is not a launched script. Its identity is used only to
    reject this bootstrap, never as an SDK context or identity fallback.
    """
    if current_turn_agent_id() is not None:
        return None
    raw = os.environ.get("AVA_AGENT_ID")  # env-ok: launched script identity channel
    if raw is None:
        return None
    return AvaContext(
        identity=AgentIdentity(agent_id=int(raw), owns_loop=False), clients=process_clients()
    )
