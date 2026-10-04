"""The reaper's remote-call loop.

TTL-expired persistent shell sessions are killed on their home machines
(`shells`). This loop dials other machines, so it runs apart from the
database-only sweep (`sweep`): a slow or unreachable machine holds up this
loop's next round, never a page expiry.
"""

from __future__ import annotations

import logging

from psycopg_pool import ConnectionPool

from base.config import settings
from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.events.live.bus import EventBus
from services.ttl_reaper import shells

_log = logging.getLogger(__name__)


async def remote_round(
    pool: ConnectionPool, db: Database, bus: EventBus, progress: LoopProgress
) -> None:
    """One pass: reclaim expired shells."""
    reaped = await shells.reap_expired_shells(pool, db, bus, progress)
    progress.beat()
    if reaped:
        _log.info("[ttl-reaper] reclaimed %d shell(s)", len(reaped))


async def remote_loop(
    pool: ConnectionPool, db: Database, bus: EventBus, progress: LoopProgress
) -> None:
    """The remote-call phases as a resident sequential loop."""

    async def one_round() -> None:
        await remote_round(pool, db, bus, progress)

    await round_loop.run_rounds(
        "remote", progress, settings.daemon.ttl_reaper_poll_interval_seconds, one_round
    )
