"""Recovery writes and notifications for dead agent-owned pages."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg_pool import AsyncConnectionPool

from base.agents.recovery import pages as page_recovery
from base.db import Database
from base.db.transaction import async_write_transaction
from base.events.live.bus import EventBus
from base.log import logger

# The notice wording, dedupe window and statements are shared with the page-server
# service's synchronous pass (`base/agents/recovery/pages.py`).
_PAGE_RECOVERY_NOTICE_PREFIX = page_recovery.NOTICE_PREFIX
_PAGE_RECOVERY_MIN_INTERVAL_S = page_recovery.MIN_INTERVAL_S
_page_recovery_notice = page_recovery.recovery_notice


async def _recent_page_recovery_notice(cur: Any, agent_id: int) -> bool:
    """Whether this agent was already told within the min interval."""
    cutoff = datetime.now(UTC) - timedelta(seconds=_PAGE_RECOVERY_MIN_INTERVAL_S)
    await cur.execute(
        page_recovery.RECENT_NOTICE_SQL,
        (agent_id, _PAGE_RECOVERY_NOTICE_PREFIX + "%", cutoff),
    )
    return await cur.fetchone() is not None


async def _close_dead_show_pages(
    pool: AsyncConnectionPool,
    db: Database,
    bus: EventBus,
    agent_id: int,
    dead: Sequence[tuple[str, int]],
    event_publisher: Any | None,
) -> None:
    """Close dead show() rows and tell the agent to re-serve them.

    A show() row (serve_dir NULL) cannot be rebuilt — its page server ran
    inside the agent's own process and died (host restart, crash, manual
    kill). The row is closed so the dead link stops showing as open; the
    agent gets one system-sourced inbound ("Page recovery: ...") listing
    every dead page of this pass, asking it to re-serve them with
    ava.ui.show() (task #2212).

    Close and notice are ONE transaction — a failure rolls back both, so the
    next heartbeat retries the pass and the agent is never told about rows
    that stayed open. The notice is deduped per agent over
    ``_PAGE_RECOVERY_MIN_INTERVAL_S``: the heartbeat runs every 5 minutes,
    so without the window a persistent failure would nag on every pass.
    PageClosed events emit after the commit.
    """
    import asyncio

    names = [name for name, _port in dead]
    notified = False
    try:
        async with async_write_transaction(pool) as conn, conn.cursor() as cur:
            for name in names:
                # CAS open->closed (the same UPDATE close_page uses).
                await cur.execute(page_recovery.CLOSE_PAGE_SQL, (agent_id, name))
            if not await _recent_page_recovery_notice(cur, agent_id):
                await cur.execute(
                    page_recovery.NOTICE_INSERT_SQL,
                    (agent_id, _page_recovery_notice(agent_id, names)),
                )
                notified = True
    except Exception:
        logger.opt(exception=True).warning(
            "page-restore: dead show-page close/notify failed",
            event="page_restore_failed",
            agent_id=agent_id,
            names=names,
        )
        return

    if event_publisher is not None:
        from base.events.live.projection import PageClosed

        for name in names:
            event_publisher.emit(PageClosed(agent_id=agent_id, name=name).model_dump_json())
    for name, port in dead:
        logger.warning(
            "page-restore: dead page without serve_dir closed",
            event="page_restore_closed",
            agent_id=agent_id,
            name=name,
            port=port,
        )
    if notified:
        # Wake the agent so the notice is claimed promptly (the claim loop's
        # SELECT recheck delivers it within timeout_s regardless). At boot the
        # listener is not subscribed yet — the wake's SETEX breadcrumb makes
        # the listener SELECT immediately on subscribe.
        from base.db import publish_inbound_wake

        await asyncio.to_thread(publish_inbound_wake, db, bus, agent_id, "0")
        logger.bind(event="page_restore_notified", agent_id=agent_id).info(
            "page-restore: told agent {agent_id} to re-serve dead show page(s) {names}",
            agent_id=agent_id,
            names=names,
        )
