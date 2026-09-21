"""Resident retrier for the deferred-delivery outbox (task #3757).

The recording half of the outbox runs in whatever process saw its send fail
(`shared.delivery_outbox.record_failed_send`); this half is the dead hand: a
daemon-lifetime task in the machine's ops server that keeps redelivering due
records — through the canonical chat-inbound path — until they land or their
budget is spent. It outlives every sender process by construction, which is
the whole point.

One pass per configured tick, each pass one `asyncio.to_thread`; a quiesced
unit (an `ava stop` draining) skips passes and keeps its records, mirroring
the shell-closure notice flusher. A config read that fails mid-life keeps the
previous wait instead of killing the loop; a failed *initial* read refuses to
start the loop at all — loudly — rather than guessing a cadence.
"""

from __future__ import annotations

import asyncio
import logging

from psycopg_pool import ConnectionPool

from shared import delivery_outbox, maintenance

_log = logging.getLogger("services.agent_ops.outbox_flusher")

_task: asyncio.Task[None] | None = None


def start(pool: ConnectionPool) -> None:
    """Begin redelivering recorded delivery failures (ops-daemon lifetime)."""
    global _task  # noqa: PLW0603 — daemon-lifetime, like the daemon's own globals
    try:
        interval = delivery_outbox.limits().flush_interval_seconds
    except Exception:
        _log.exception("[delivery-outbox] flusher not started: initial config read failed")
        return
    _task = asyncio.create_task(_run(pool, interval))


def stop() -> None:
    """Cancel the loop; records stay for the next start."""
    global _task  # noqa: PLW0603
    if _task is not None:
        _task.cancel()
        _task = None


async def _run(pool: ConnectionPool, interval: float) -> None:
    while True:
        if not maintenance.quiesced():
            try:
                report = await asyncio.to_thread(delivery_outbox.flush, pool)
            except Exception:
                _log.exception("[delivery-outbox] flush pass failed; records kept")
            else:
                if report.touched or report.expired:
                    _log.info(
                        "[delivery-outbox] flush pass: delivered={} buffered={} abandoned={} "
                        "deferred={} unreadable={} expired={}",
                        report.delivered,
                        report.buffered,
                        report.abandoned,
                        report.deferred,
                        report.unreadable,
                        report.expired,
                    )
        await asyncio.sleep(interval)
        try:
            interval = (await asyncio.to_thread(delivery_outbox.limits)).flush_interval_seconds
        except Exception:
            _log.exception("[delivery-outbox] tick interval read failed; keeping {}s", interval)
