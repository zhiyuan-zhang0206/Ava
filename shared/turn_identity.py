"""Turn-scoped agent identity — the contextvar half of "who am I".

Phase 1 of `future/infra/agent-runner-as-server.md`: in the hosted runner many
agents' turns share one process, so the two process-level identity channels —
`ava._boot._agent_id` (set once by the agent bootstrap) and the `AVA_AGENT_ID`
environment variable (set once for launched children) — cannot distinguish
which agent's turn is executing. This module holds the third, innermost
channel: a contextvar the hosted dispatcher binds before creating an agent's
turn task. `asyncio.create_task` copies the creating context, and LangGraph
node tasks copy the loop-level context, so one bind at task creation covers
every node of the turn; exec worker threads are started under
`contextvars.copy_context()` (see `agent/graph/_exec.py`) so agent code on the
worker thread sees the same identity.

Resolution order everywhere identity is read:

    turn contextvar  >  process bootstrap slot (`ava._boot`)  >  AVA_AGENT_ID env

Process mode binds nothing here, the contextvar stays None, and every read
falls through to the process slot / env — behavior unchanged.

This lives in `shared/` (not `ava/`) because identity consumers exist below
the `ava` layer (`shared/lm/_providers.py` cache affinity, `shared/resilience.py`
retry de-phasing) and the import layering is `shared < ava`. `ava._boot`
layers its process slot on top of this module's read.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Generator
from contextvars import ContextVar

_TURN_AGENT_ID: ContextVar[int | None] = ContextVar("ava_turn_agent_id", default=None)


@contextlib.contextmanager
def bind_turn_identity(agent_id: int) -> Generator[None, None, None]:
    """Bind `agent_id` as the current context's turn identity.

    The hosted dispatcher wraps turn-task creation in this (together with
    `shared.config.bind_agent_config`): bind -> create the task (which copies
    the context) -> reset. Nesting rebinds cleanly.
    """
    token = _TURN_AGENT_ID.set(agent_id)
    try:
        yield
    finally:
        _TURN_AGENT_ID.reset(token)


def current_turn_agent_id() -> int | None:
    """The turn identity bound in the current context, or None (process mode)."""
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
