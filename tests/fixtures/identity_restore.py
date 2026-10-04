"""Put the agent identity back after every test.

Hundreds of tests pin the agent they created with `pin_agent(spawn_agent())` (`tests/fixtures/pin_agent.py`), which binds an
`AvaContext` carrying that identity for the process (`ava.sdk_surface.process_context`), and nothing
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

import sys
from collections.abc import Iterator

import pytest

# (module, name): ContextVars, read through `.get()` in the main thread's context. The first is the
# process's AvaContext; add one only when a test can leave it changed.
IDENTITY_CONTEXTVARS: tuple[tuple[str, str], ...] = (
    ("ava.sdk_surface.process_context", "_CURRENT"),
    ("base.native_process.turn_identity", "_TURN_AGENT_ID"),
)


@pytest.fixture(autouse=True)
def _restore_agent_identity() -> Iterator[None]:
    variables = [getattr(sys.modules[module], name) for module, name in IDENTITY_CONTEXTVARS]
    held_variables = [variable.get() for variable in variables]
    context_module = sys.modules.get("ava.sdk_surface.process_context")
    context_var = getattr(context_module, "_CURRENT", None)
    held = None if context_var is None else context_var.get()
    yield
    # Clients a test's own context built end with the test; the ones that were bound before it
    # (the session default's, which `pin_agent` carries over) keep living.
    current = None if context_var is None else context_var.get()
    if current is not None and (held is None or current.clients is not held.clients):
        current.clients.close()
    for variable, value in zip(variables, held_variables, strict=True):
        variable.set(value)
