"""Put the agent identity back after every test.

Hundreds of tests pin the agent they created with a bare assignment,
`ava.agent_identity._agent_id = spawn_agent()` (the pattern `env_bootstrap` documents), and
nothing puts the previous value back. The next test in the same xdist worker then reads an
identity it never set, so which tests fail depends on the shard composition, and moving test
files changes it. This fixture reads the identity slots before a test and writes them back after
the test's own fixtures are torn down, so no test has to undo the convention.

It is function-scoped autouse. The leak guard does not compare these slots, so no order against the
guard matters; it is registered ahead of the other autouse plugins so that its window covers what they do.

`ava.self.AGENT_ID` is not touched. The module `__getattr__` serves it from these slots, and a name
it serves must never be assigned: writing back what was read would store it as a real attribute for
good (PR #3791).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest

# (module, attrs): the process-global identity slots. Add one only when a test can leave it changed.
IDENTITY_SLOTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "ava.agent_identity",
        ("_agent_id", "_owns_loop", "_actor", "_external_agent_id", "_external_identity"),
    ),
)
# (module, name): ContextVars, read through `.get()` in the main thread's context.
IDENTITY_CONTEXTVARS: tuple[tuple[str, str], ...] = (
    ("base.native_process.turn_identity", "_TURN_AGENT_ID"),
)


@pytest.fixture(autouse=True)
def _restore_agent_identity() -> Iterator[None]:
    slots = [(sys.modules[module], attr) for module, attrs in IDENTITY_SLOTS for attr in attrs]
    held = [getattr(module, attr) for module, attr in slots]
    variables = [getattr(sys.modules[module], name) for module, name in IDENTITY_CONTEXTVARS]
    held_variables = [variable.get() for variable in variables]
    yield
    for (module, attr), value in zip(slots, held, strict=True):
        setattr(module, attr, value)
    for variable, value in zip(variables, held_variables, strict=True):
        variable.set(value)
