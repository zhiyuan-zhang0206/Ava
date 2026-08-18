"""Low-level helpers / constants shared by graph nodes internally.

Not exposed to the `agent.graph` public API (`__init__.py` does not
re-export) — these are node implementation details, not for external use.
"""

import asyncio

# Exception tuples for bound except — under Python 3.14, ruff format strips
# the parens on inline `except (A, B):` to use PEP 758 syntax, which
# visually looks like Py2 old-style `except A, B` and misleads readers.
# Extracting to module-level named tuples satisfies both ruff and reader
# intuition.
_STDIN_PIPE_CLOSED = (BrokenPipeError, ConnectionResetError)
_DRAIN_SHUTDOWN_ERRORS = (TimeoutError, asyncio.CancelledError)


def _uncancel_current_task() -> None:
    """Swallow `task.cancel()` cancellation request — after catching
    CancelledError, the next await (langgraph writing checkpoint etc.) does
    not raise again.

    Python 3.11+ Task carries a cancellation counter; each `task.cancel()`
    increments, `task.uncancel()` decrements. Catching CancelledError
    without calling uncancel makes the next await point re-raise. This
    helper centralizes "I want to swallow this cancel and keep returning".
    """
    task = asyncio.current_task()
    if task is not None:
        task.uncancel()
