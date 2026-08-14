"""Framework lifecycle signals — the exceptions that drive turn-stopping.

These live in `shared` (below both the agent kernel and the `ava` SDK) so the
kernel can import them without depending on the agent-facing `ava.self` module.
`ava.self` re-exports them and its `restart()` / `terminate()` / `compact()` /
`update()` functions raise them; the exec node catches them to end a turn. That
separation lets a context like a benchmark runner disable `ava.self` wholesale
(via AVA_SDK_DISABLE) without breaking the kernel's import of these signals.
"""

from __future__ import annotations

from shared.exit_codes import IDLE_EXIT_CODE, SYSTEM_HALT_EXIT_CODE

__all__ = ["AgentRestart", "AgentTermination", "_LifecycleExit", "_SystemHalt"]


class _LifecycleExit(SystemExit):
    """Private base for framework lifecycle signals — inherits SystemExit
    (not Exception), so broad try/except Exception won't accidentally
    swallow lifecycle transitions."""


class AgentTermination(_LifecycleExit):
    """Raised on the success path of `terminate()`. Your process exits and
    has to be woken up by sending it a chat message (auto-resurrect)."""

    def __init__(self) -> None:
        super().__init__(IDLE_EXIT_CODE)


class AgentRestart(_LifecycleExit):
    """Raised on the success path of `restart()` and `update()`. Your
    process exits and a fresh one comes up under the same agent id."""

    def __init__(self) -> None:
        super().__init__(IDLE_EXIT_CODE)


class _SystemHalt(_LifecycleExit):
    """Raised by `compact()` — framework takes over history compaction. Agent doesn't raise directly."""

    def __init__(self) -> None:
        super().__init__(SYSTEM_HALT_EXIT_CODE)
