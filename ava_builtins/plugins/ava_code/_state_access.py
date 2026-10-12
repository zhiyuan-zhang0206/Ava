"""The ava_code surface's access to its own plugin state.

The state class and the real handle live in the agent-runtime face (`agent_runtime.py`), loaded when
a state slot first materializes. The surface (`plugin.py`, the `ava.cwd` namespace in
`_code_namespace.py`) reaches the state only through `read_state` / `update_state`, which resolve the
handle at each call; this module imports neither, so both can depend on it.
"""

from __future__ import annotations

from typing import Any

import ava


# The state class and the real handle live in the agent-runtime face
# (`agent_runtime.py`), loaded when a state slot first materializes (task #3633
# leg-2: a stateful child's slot is lazy). The surface reaches the state only
# through `read_state` / `update_state`, which resolve the handle at each call:
# with a live slot (`ava.in_exec_turn()`), the slot exposes `materialize()` —
# the framework contract on the lazy slot — which loads the face, and the call
# then runs on the real handle. Without a live slot — outside a turn (test / dev
# REPL) — they raise exactly what the real handle raises outside an exec turn,
# without importing the agent runtime.
def _handle(method: str, slot: str) -> Any:
    """The face's handle, with a live lazy state slot materialized first."""
    if not ava.in_exec_turn():
        raise ava.PluginStateOutsideTurnError(
            f"PluginStateHandle[AvaCodeState].{method}() called outside exec turn—"
            f"{slot} only valid inside execute_code (the exec turn)."
        )
    materialize = getattr(ava.state, "materialize", None)
    if materialize is not None:
        materialize()
    from .agent_runtime import state_handle

    return state_handle()


def read_state() -> Any:
    """The plugin's current `AvaCodeState` snapshot."""
    return _handle("read", "ava.state").read()


def update_state(delta: dict[str, Any]) -> None:
    """Apply a field update to the plugin's state."""
    _handle("update", "ava.state_update").update(delta)
