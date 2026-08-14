# pyright: reportUnnecessaryTypeIgnoreComment=true, reportUnusedClass=false
"""Static (pyright) contract for the `Hook` base class — a pyright-only module,
NOT collected by pytest (the filename does not match `test_*`).

The payoff of moving hooks onto a typed base class is that the `__call__`
signature is enforced by the type checker: a subclass whose override does not
match is a pyright `reportIncompatibleMethodOverride` error, so a plugin author
learns at check time rather than at runtime. This file proves that enforcement
is *live*.

Each `_Bad*` subclass overrides `__call__` with a signature that violates the
Liskov substitution rules pyright checks (narrowed parameter, widened return,
dropped parameter). Each carries a `# pyright: ignore[reportIncompatibleMethodOverride]`
on its `async def __call__` line, and the file-level
`reportUnnecessaryTypeIgnoreComment=true` above makes those ignores load-bearing:
if the base class ever stops pinning the signature, the override errors vanish,
the ignores become *unnecessary*, and pyright fails on this file. So a regression
in the contract turns CI red here.

`_Ok` is the happy path — a correct override, no ignore, type-checks clean.

To see the errors directly, delete an ignore comment and run:
    .venv/bin/pyright tests/agent/hook_typing_contract.py
"""

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph._context import AvaContext
from agent.hooks import Hook
from agent.state import AgentState


class _Ok(Hook):
    """Correct override — matches the base signature exactly, no ignore needed."""

    async def __call__(
        self,
        state: AgentState,
        runtime: Runtime[AvaContext],
        config: RunnableConfig,
        /,
    ) -> dict | None:
        return None


class _BadParamType(Hook):
    """`state` narrowed to `int` — a parameter-contravariance violation."""

    async def __call__(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        state: int,
        runtime: Runtime[AvaContext],
        config: RunnableConfig,
        /,
    ) -> dict | None:
        return None


class _BadReturn(Hook):
    """Return widened to `object` — a return-covariance violation."""

    async def __call__(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        state: AgentState,
        runtime: Runtime[AvaContext],
        config: RunnableConfig,
        /,
    ) -> object:
        return None


class _BadArity(Hook):
    """Drops the `config` parameter — fewer positional params than the base."""

    async def __call__(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        state: AgentState,
        runtime: Runtime[AvaContext],
        /,
    ) -> dict | None:
        return None
