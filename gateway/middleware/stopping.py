"""Whether this gateway process has begun to shut down, for long-lived streams.

uvicorn cancels an unfinished response only when its connection-drain budget
(`timeout_graceful_shutdown`) runs out, so a stream that never ends by itself
holds every stop for the whole budget, and an SSE stream that stays open for
hours always does. `gateway._server.GatewayServer` marks the moment
uvicorn enters its shutdown; a stream polls `is_stopping()` between frames and
ends by itself, so its client reconnects to the next gateway at once instead of
after the budget. The drain budget stays the bound for whatever does not end.
"""

from __future__ import annotations

import threading

_stopping = threading.Event()


def mark_stopping() -> None:
    """Record that this process's server is shutting down; it never clears."""
    _stopping.set()


def is_stopping() -> bool:
    """Whether a long-lived response should end now."""
    return _stopping.is_set()
