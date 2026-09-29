"""Deliver the previous stop's shell-closure notices (issue #2044).

Split out of `daemon.py` (its file-size ceiling crossed): one daemon-lifetime
task delivers the shell-closure notices the previous stop recorded, once its
unit's maintenance hold has released, with bounded retries; undelivered
records stay in place for the next start.
"""

from __future__ import annotations

import asyncio
import logging

from psycopg_pool import ConnectionPool

from base.deploy.maintenance import admission
from ops import pty_close_notices

_log = logging.getLogger("services.agent_ops.close_notices")

_delivery_task: asyncio.Task[None] | None = None
# How often the flush re-reads the durable hold while its unit is quiesced.
_ADMISSION_POLL_S = 5.0


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

    Every managed start (`ava start` after `ava stop`, a release or PITR
    start) runs this daemon inside the maintenance hold it releases only after
    readiness. Each attempt first waits for that release: borrowing the pool
    inside the stop window would open the exact client connections the stop
    just released, and giving up there would leave the records to a start
    that is never outside a hold. The wait has no deadline of its own — the
    hold's owner bounds it, and the task ends with the daemon. Undelivered
    records stay in place for the next start (issue #2044).
    """
    for delay in (0.0, 30.0, 120.0, 300.0):
        if delay:
            await asyncio.sleep(delay)
        await _admitted()
        try:
            remaining = await asyncio.to_thread(pty_close_notices.flush, pool)
        except Exception:
            _log.exception("[ops] shell-closure notice flush failed; records kept")
            continue
        if not remaining:
            return
        _log.warning("[ops] %d shell-closure notices undelivered; retrying", remaining)
    _log.error("[ops] shell-closure notices undelivered after retries; kept for next start")


async def _admitted() -> None:
    """Return once this unit is outside its quiesced stop window."""
    while admission.quiesced():
        await asyncio.sleep(_ADMISSION_POLL_S)
