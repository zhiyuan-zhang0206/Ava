"""Put the agent identity back after every test.

Hundreds of tests pin the agent they created with `pin_agent(spawn_agent())` (`tests/fixtures/pin_agent.py`), which binds an
`AvaContext` carrying that identity in the ordinary `ava.context` module slot, and nothing
puts the previous one back. The next test in the same xdist worker then reads an identity it never
set, so which tests fail depends on the shard composition, and moving test files changes it. This
fixture reads the bound context before a test and writes it back after the test's own fixtures are
torn down, so no test has to undo the convention.

It is function-scoped autouse. The leak guard does not compare it, so no order against the
guard matters; it is registered ahead of the other autouse plugins so that its window covers what they do.

`ava.self.AGENT_ID` is not touched: the module `__getattr__` serves it from the bound identity, and
a name it serves must never be assigned: writing back what was read would store it as a real
attribute for good (PR #3791).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

import pytest

__all__ = ["SdkIdentityClients", "SdkIdentityContext", "SdkIdentitySlot"]


class SdkIdentityClients(Protocol):
    """Clients the identity guard can close when a test owns their context."""

    def close(self) -> None: ...


class SdkIdentityContext(Protocol):
    """The client identity needed to distinguish owned and borrowed contexts."""

    @property
    def clients(self) -> SdkIdentityClients: ...


class SdkIdentitySlot[Context: SdkIdentityContext](Protocol):
    """The writable context slot, preserving its implementation's context type."""

    context: Context


def _bound_context[Context: SdkIdentityContext](
    slot: SdkIdentitySlot[Context],
) -> Context | None:
    try:
        return slot.context
    except AttributeError:
        return None


@pytest.fixture
def sdk_identity() -> SdkIdentitySlot[SdkIdentityContext] | None:
    """An unrelated test has no SDK context slot; consumers override this locally."""
    return None


@pytest.fixture(autouse=True)
def _restore_agent_identity[Context: SdkIdentityContext](
    sdk_identity: SdkIdentitySlot[Context] | None,
) -> Iterator[None]:
    if sdk_identity is None:
        yield
        return
    held = _bound_context(sdk_identity)
    yield
    # Clients a test's own context built end with the test; the ones that were bound before it
    # (the session default's, which `pin_agent` carries over) keep living.
    current = _bound_context(sdk_identity)
    try:
        if current is not None and (held is None or current.clients is not held.clients):
            current.clients.close()
    finally:
        if held is None:
            delattr(sdk_identity, "context")
        else:
            sdk_identity.context = held
