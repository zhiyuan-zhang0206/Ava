"""The plugin-state slot of the turn this process is running.

An exec child (or an external attachment) binds the turn's working state copy and the delta
accumulated against it; `agent.state.PluginStateHandle` reads and writes through the same two
values, because the SDK functions plugins call take no context argument. The slot belongs to the
installation: `ava.sdk_surface.install.install` creates it, `uninstall` drops it, and
`install.turn_state()` reads it back.

Framework-internal: not agent-facing, never in the `ava.help()` view.
"""

from __future__ import annotations

from typing import Any


class TurnState:
    """The bound working copy of the turn's state and its accumulated raw delta."""

    def __init__(self) -> None:
        self.state: Any = None
        self.update: dict[str, Any] | None = None
