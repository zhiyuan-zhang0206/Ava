"""Recovery writes and notifications for dead agent-owned pages."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg_pool import AsyncConnectionPool

from shared.db_transaction import async_write_transaction
from shared.log import logger

# The re-serve notice prefix — also the dedupe key for the min-interval check.
_PAGE_RECOVERY_NOTICE_PREFIX = "Page recovery:"
# Repeated heartbeats (every 5 min) while a dead row survives must not nag
# the agent; the interval is checked against the agent's own inbound history.
_PAGE_RECOVERY_MIN_INTERVAL_S = 6 * 3600


def _page_recovery_notice(agent_id: int, names: list[str]) -> str:
    """The re-serve notice content; the prefix doubles as the dedupe key."""
    return (
        f"{_PAGE_RECOVERY_NOTICE_PREFIX} page(s) "
        f"{', '.join(repr(n) for n in names)} of agent {agent_id} are no longer "
        "being served (their page server died). Re-serve them with "
        "ava.ui.show() to republish."
    )


async def _recent_page_recovery_notice(cur: Any, agent_id: int) -> bool:
    """Whether this agent was already told within the min interval."""
    cutoff = datetime.now(UTC) - timedelta(seconds=_PAGE_RECOVERY_MIN_INTERVAL_S)
    await cur.execute(
        "SELECT 1 FROM inbound_messages "
        "WHERE agent_id = %s AND source = 'system' "
        "AND content LIKE %s AND created_at > %s LIMIT 1",
        (agent_id, _PAGE_RECOVERY_NOTICE_PREFIX + "%", cutoff),
    )
    return await cur.fetchone() is not None


async def _close_dead_show_pages(
    pool: AsyncConnectionPool,
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
                await cur.execute(
                    "UPDATE agent_pages SET closed_at = now() "
                    "WHERE agent_id = %s AND name = %s AND closed_at IS NULL "
                    "AND expired_at IS NULL",
                    (agent_id, name),
                )
            if not await _recent_page_recovery_notice(cur, agent_id):
                await cur.execute(
                    "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                    "VALUES (%s, %s, 'chat', 'system')",
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
        from shared.live_events import PageClosed

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
        from shared.db import publish_inbound_wake

        await asyncio.to_thread(publish_inbound_wake, agent_id, "0")
        logger.bind(event="page_restore_notified", agent_id=agent_id).info(
            "page-restore: told agent {agent_id} to re-serve dead show page(s) {names}",
            agent_id=agent_id,
            names=names,
        )
