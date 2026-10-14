"""Bind (or unbind) the SDK's agent identity for one test, for the components below the `ava` facade.

Tests of `ava.shell`, `ava.skills` and `ava.mcps` cannot import the `ava/tests` SDK fixtures (those import
`ava`, which imports them), and they only need to know which agent the code acts for. These fixtures
bind a context that carries that identity and no live clients, and put the previous binding back.
"""

from collections.abc import Iterator
from typing import Protocol

import pytest

from ava.sdk_surface import process_context
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity

__all__ = ["BindAgent", "bound_agent", "no_agent"]


class BindAgent(Protocol):
    """Bind a context acting as `agent_id`, carrying `clients` when given."""

    def __call__(self, agent_id: int, *, clients: ClientSet | None = None) -> None: ...


@pytest.fixture
def bound_agent() -> Iterator[BindAgent]:
    """Bind a context acting as the given agent; the previous binding returns after the test."""
    previous = process_context.unbind_context()

    def bind(agent_id: int, *, clients: ClientSet | None = None) -> None:
        identity = AgentIdentity(agent_id=agent_id, owns_loop=True)
        process_context.bind_context(
            AvaContext(identity=identity)
            if clients is None
            else AvaContext(identity=identity, clients=clients)
        )

    try:
        yield bind
    finally:
        process_context.unbind_context()
        if previous is not None:
            process_context.bind_context(previous)


@pytest.fixture
def no_agent() -> Iterator[None]:
    """Run the test with no SDK context bound (a process that is no agent)."""
    previous = process_context.unbind_context()
    try:
        yield
    finally:
        process_context.unbind_context()
        if previous is not None:
            process_context.bind_context(previous)
