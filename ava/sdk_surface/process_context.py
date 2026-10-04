"""The process's `AvaContext` — the one in-process way to ask "what am I running as".

`ava.*` is a namespace of free functions that agent code calls inside its exec child (or a script
an agent launched), so nothing can hand them the run's context. They read it here, the way
LangGraph's `get_runtime()` reads the run's `Runtime`: one `ContextVar`. Everything else a function
needs is derived from the context it finds; nothing else in `ava/` holds state.

Who binds it:

- the exec child, once, from the description in its request envelope (`bind_process`), releasing
  its clients when it ends (`close_process`);
- a script an agent launched, lazily on first read, from `AVA_AGENT_ID` in its environment
  (`owns_loop=False`: it carries the agent's identity but is not the agent's turn path);
- a gateway-hosted schedule runner, which has an actor and no agent;
- an external attachment, for the lifetime of the attachment (`bind_process` / `unbind_process`);
- a test, with `scoped`.

The agent host binds each turn's context around that turn's graph run (`scoped`), so an `ava.*`
call a graph node makes reaches the host's shared clients; the host never binds one for the
process, since it serves many agents. Where nothing is bound (a bare script, the host between
turns), `ava.context` raises `ContextOutsideProcessError`, as `ava.state` raises outside an exec
turn.

A `ContextVar` is per-context: a plain thread starts with an empty one. A process that binds with
`bind_process` makes the threads it starts afterwards carry the context bound at `start()` (this
variable only), so agent code that fans work out with a thread pool keeps seeing its own context.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import functools
import os
import threading
from collections.abc import Generator
from typing import Any

from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.native_process.turn_identity import current_turn_agent_id

_CURRENT: contextvars.ContextVar[AvaContext | None] = contextvars.ContextVar(
    "ava_context", default=None
)


class ContextOutsideProcessError(AttributeError):
    """`ava.context` read where no context is bound.

    An AttributeError, so the attribute simply does not exist there: the agent host (many agents
    share it), a bare script an agent did not launch, a thread started before the context was
    bound. Code that needs the identity of a run reads the context its caller handed it.
    """


def process_clients(*, gateway_url: str | None = None) -> ClientSet:
    """The clients of a process's own context: its database is the cluster's, as its settings
    name it. The composition root of every context this process builds for itself."""
    from ava import _settings

    return ClientSet(gateway_url=gateway_url, database=_settings.database)


def context_from_description(description: dict[str, Any]) -> AvaContext:
    """The context an exec child builds from its request envelope's description."""
    from ava import _settings

    return AvaContext.from_description(description, database=_settings.database)


def _launched_child_context() -> AvaContext | None:
    """The context of a script an agent launched: its identity is in the environment.

    Inside a host turn the turn contextvar answers, so nothing is derived there."""
    if current_turn_agent_id() is not None:
        return None
    raw = os.environ.get("AVA_AGENT_ID")  # env-ok: the identity channel of a launched child
    if raw is None:
        return None
    context = AvaContext(
        identity=AgentIdentity(agent_id=int(raw), owns_loop=False), clients=process_clients()
    )
    bind_process(context)
    # A script has no end hook of its own: its clients are released when the interpreter exits.
    atexit.register(context.clients.close)
    return context


def peek() -> AvaContext | None:
    """The bound context, or None where there is none."""
    context = _CURRENT.get()
    return context if context is not None else _launched_child_context()


def current() -> AvaContext:
    """The bound context; raises `ContextOutsideProcessError` where there is none."""
    context = peek()
    if context is None:
        raise ContextOutsideProcessError(
            "ava.context exists only in a process that runs as an agent: the exec child of a turn "
            "(execute_code), a script an agent launched, or an attached external controller. "
            "This process has no bound context; code outside one reads the context it was handed."
        )
    return context


def _share_context_with_threads() -> None:
    """Threads started from here on run with the context their starter has bound.

    Only this one variable crosses: every other contextvar (the host's turn identity among them)
    keeps Python's rule that a thread starts empty."""
    start = threading.Thread.start
    if getattr(start, "_ava_shares_context", False):
        return

    @functools.wraps(start)
    def start_with_context(thread: threading.Thread) -> None:
        bound = _CURRENT.get()
        if bound is not None:
            run = thread.run

            def run_with_context() -> None:
                _CURRENT.set(bound)
                run()

            thread.run = run_with_context  # type: ignore[method-assign]
        start(thread)

    start_with_context._ava_shares_context = True  # type: ignore[attr-defined]
    threading.Thread.start = start_with_context  # type: ignore[method-assign]


def bind_process(context: AvaContext) -> None:
    """Bind `context` for the rest of this process: the exec child's boot, a launched script."""
    _CURRENT.set(context)
    _share_context_with_threads()


def close_process() -> None:
    """Release the clients of the context this process bound (the exec child's end)."""
    context = _CURRENT.get()
    if context is not None:
        context.clients.close()


def unbind_process() -> None:
    """Drop the context bound by `bind_process` (an external attachment's detach)."""
    _CURRENT.set(None)


@contextlib.contextmanager
def scoped(context: AvaContext | None) -> Generator[None]:
    """Bind `context` for the block (None: no context), then put back what was bound."""
    token = _CURRENT.set(context)
    try:
        yield
    finally:
        _CURRENT.reset(token)
