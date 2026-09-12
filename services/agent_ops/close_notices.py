"""Deliver the previous stop's shell-closure notices (issue #2044).

Split out of `daemon.py` (its file-size ceiling crossed): one daemon-lifetime
task delivers the shell-closure notices the previous stop recorded, with
bounded retries; undelivered records stay in place for the next start.
"""

from __future__ import annotations

import asyncio
import logging

from psycopg_pool import ConnectionPool

from ops import pty_close_notices
from shared import maintenance

_log = logging.getLogger("services.agent_ops.close_notices")

_delivery_task: asyncio.Task[None] | None = None


def start(pool: ConnectionPool) -> None:
    """Begin delivering shell-closure notices recorded by the previous stop."""
    global _delivery_task  # noqa: PLW0603 — daemon-lifetime, like the daemon's own globals
    _delivery_task = asyncio.create_task(deliver(pool))


def stop() -> None:
    """Cancel the delivery task; records stay for the next start."""
    global _delivery_task  # noqa: PLW0603
    if _delivery_task is not None:
        _delivery_task.cancel()
        _delivery_task = None


async def deliver(pool: ConnectionPool) -> None:
    """Deliver the previous stop's shell-closure notices; bounded retries.

    Undelivered records stay in place for the next start (issue #2044).
    A quiesced unit skips the flush entirely and keeps its records: borrowing
    the pool would open the exact client connections the stop just released.
    """
    for delay in (0.0, 30.0, 120.0, 300.0):
        if delay:
            await asyncio.sleep(delay)
        if maintenance.quiesced():
            return
        try:
            remaining = await asyncio.to_thread(pty_close_notices.flush, pool)
        except Exception:
            _log.exception("[ops] shell-closure notice flush failed; records kept")
            continue
        if not remaining:
            return
        _log.warning("[ops] %d shell-closure notices undelivered; retrying", remaining)
    _log.error("[ops] shell-closure notices undelivered after retries; kept for next start")
