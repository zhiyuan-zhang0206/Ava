"""The reaper's remote-call loop: shell kills and work-failure redelivery.

Both phases dial other machines or processes, so they run apart from the
database-only sweep (`sweep`): a slow or unreachable machine holds up this
loop's next round, never a page expiry.

- **Shells** — TTL-expired persistent shell sessions are killed on their home
  machines (`shells`).
- **Work failures** — a gateway crash after recording a `work_failed_events` row
  but before finishing its route is retried through the original
  author/delegator/task fallback chain
  (`gateway.routers.work_failed.reconcile_stale_work_failures`; each event's
  delivery is bounded by an RPC deadline there).
"""

from __future__ import annotations

import logging

from psycopg_pool import ConnectionPool

from base.config import settings
from base.daemon.loop_health import LoopProgress
from gateway.routers import work_failed as work_failed_router
from services.delivery_watchdog import rounds
from services.ttl_reaper import shells

_log = logging.getLogger(__name__)


async def remote_round(pool: ConnectionPool, progress: LoopProgress) -> None:
    """One pass: reclaim expired shells, then redeliver stale work failures."""
    reaped = await shells.reap_expired_shells(pool, progress)
    progress.beat()
    failures = await work_failed_router.reconcile_stale_work_failures(pool, on_event=progress.beat)
    if reaped or failures:
        _log.info(
            "[ttl-reaper] reclaimed %d shell(s); completed %d stale work failure(s)",
            len(reaped),
            failures,
        )


async def remote_loop(pool: ConnectionPool, progress: LoopProgress) -> None:
    """The remote-call phases as a resident sequential loop."""

    async def one_round() -> None:
        await remote_round(pool, progress)

    await rounds.run_rounds(
        "remote", progress, settings.daemon.ttl_reaper_poll_interval_seconds, one_round
    )
