"""The reaper's database-only sweep: expired pages, browser sessions, notices and
impersonation leases, plus the slow maintenance phases on durable cadences.

One resident loop (`sweep_loop`) runs `sweep_round` every
`AVA_TTL_REAPER_POLL_INTERVAL_SECONDS`. A round runs its phases in order; each
phase is a bounded batch, and a backlog drains over successive rounds:

- **Pages** — the row is terminalized with ``expired_at`` (the reverse proxy
  then answers the page's link with the friendly "page expired" notice), a
  ``PageClosed`` event is published so the frontend drops the entry, and the
  page-server daemon stops the page session when its reconcile sees the row
  leave the open set (the daemon's existing two-layer teardown). Pages have a
  hard lifetime (user ruling 2026-08-25; default 24h, ``AVA_PAGE_DEFAULT_TTL_SECONDS``).
- **Browser sessions** — expired ``web_sessions`` rows are deleted here, so
  cleanup does not depend on the next login.
- **Notices** — ``agent_notices`` past ``expire_at`` auto-resolve.
- **Impersonation** — expiring leases are reminded, expired ones reaped, and ended
  leases still waiting for an open event source raise (or clear) their seal-stuck alert.
- **Schedule fire log** — ``schedule_fire_log`` claims older than the configured
  retention window (30 days by default) are deleted once per day, keeping the
  newest claim per schedule so the catch-up baseline never regresses.
- **Lifecycle pointers** — the hourly torn-pointer scan and absent-machine
  fence settle (``lifecycle_fences``).

The slow phases are claimed in ``maintenance_state`` (``cadence``), so a restart
resumes their cadence. Owners are notified (inbound, source ``"system"``) only
when the agent is running or idling — a terminated agent's page expiring is
exactly the cleanup the TTL exists for, and must not resurrect it.

Every reclaim is a CAS that loses cleanly to ``close()`` / termination. The sweep
has no remote calls; shell kills and work-failure redelivery live in the
``remote`` loop so a slow machine never delays a page expiry.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents.impersonation.maintenance import (
    alert_stuck_event_logs,
    reap_impersonations,
    remind_expiring_impersonations,
)
from base.config import settings
from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.db.transaction import write_transaction
from base.events.live.announce import publish_agent_updated_sync
from base.events.live.bus import EventBus
from base.events.live.projection import PageClosed
from base.events.live.redis_client import publish_best_effort_sync
from ops import lifecycle
from services.ttl_reaper import cadence
from services.ttl_reaper.lifecycle_fences import (
    _scan_torn_lifecycle_pointers_blocking,
    settle_absent_machine_fences,
)
from services.ttl_reaper.owner_notice import PASS_BATCH, notify_owner

_log = logging.getLogger(__name__)

# Bound for the per-pass schedule_fire_log retention prune. The tables are
# small by design; the cap keeps a backlog (e.g. after a long outage) from
# turning one pass into a multi-minute transaction.
_FIRE_LOG_PASS_BATCH = 50_000


def _reap_expired_notices_blocking(pool: ConnectionPool) -> list[tuple[int, int]]:
    """Auto-resolve notices whose expire_at deadline has elapsed; return (agent_id, notice_id)."""
    with write_transaction(pool) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, agent_id, title, require_response FROM agent_notices "
                "WHERE expire_at <= now() AND resolved_at IS NULL "
                "ORDER BY expire_at, id LIMIT %s "
                "FOR UPDATE SKIP LOCKED",
                (PASS_BATCH,),
            )
            rows = cur.fetchall()
        if not rows:
            return []
        reaped: list[tuple[int, int]] = []
        updated_agents: set[int] = set()
        for nid, agent_id, title, require_response in rows:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_notices SET resolved_at = now(), resolution = 'expired' "
                    "WHERE id = %s AND resolved_at IS NULL",
                    (nid,),
                )
                if cur.rowcount == 0:
                    continue
            if require_response:
                updated_agents.add(agent_id)
            notify_owner(
                conn,
                agent_id,
                f'Re: "{title}"\n\n[This notice has expired.]',
                source="system:notice-expire",
            )
            reaped.append((agent_id, nid))
    for aid in updated_agents:
        with suppress(Exception):
            publish_agent_updated_sync(EventBus.from_settings(), aid)
    return reaped


def _reap_expired_pages_blocking(pool: ConnectionPool) -> list[tuple[int, str, int]]:
    """Terminalize page rows past their deadline; return (agent_id, name, id).

    Runs on one connection (via to_thread — the gateway event loop never
    blocks on psycopg). Each row is a CAS so an explicit close() or a parallel
    reaper pass wins the race instead of double-terminalizing.
    """
    with write_transaction(pool) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, agent_id, name, serve_dir FROM agent_pages "
                "WHERE expires_at IS NOT NULL AND expires_at <= now() "
                "AND closed_at IS NULL AND expired_at IS NULL "
                "ORDER BY id LIMIT %s",
                (PASS_BATCH,),
            )
            rows = cur.fetchall()
        reaped: list[tuple[int, str, int]] = []
        for page_id, agent_id, name, serve_dir in rows:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_pages SET expired_at = now() "
                    "WHERE id = %s AND closed_at IS NULL AND expired_at IS NULL",
                    (page_id,),
                )
                if cur.rowcount == 0:
                    continue
            reaped.append((agent_id, name, page_id))
            if serve_dir is None:
                # show() page: the gateway only expires the registration — the
                # server lives in the agent's own process, so the notice tells
                # the owner to stop it and release the port.
                notify_owner(
                    conn,
                    agent_id,
                    f"Page {name!r} (agent {agent_id}) was reclaimed after its TTL "
                    "expired. Stop the page's HTTP server to release its port; "
                    "re-show with ava.ui.show() to republish.",
                )
            else:
                notify_owner(
                    conn,
                    agent_id,
                    f"Page {name!r} (agent {agent_id}) was reclaimed after its TTL "
                    "expired. Serve it again with ava.ui.serve() to republish.",
                )
            telemetry.emit(
                "log",
                "page_ttl_expired",
                level="info",
                agent_id=agent_id,
                attributes={"agent_id": agent_id, "name": name, "page_id": page_id},
            )
    # PageClosed is a live-UI event: publish outside the DB transaction so a
    # Redis hiccup cannot roll back the terminal UPDATE.
    for agent_id, name, _page_id in reaped:
        event = PageClosed(agent_id=agent_id, name=name)
        publish_best_effort_sync(
            settings.data_plane.events_channel,
            event.model_dump_json(),
            context="ttl_reaper_page",
        )
    return reaped


def _reap_expired_web_sessions_blocking(pool: ConnectionPool) -> int:
    """Delete browser sessions whose authoritative expiry has elapsed."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "WITH expired AS ("
            "SELECT id FROM web_sessions WHERE expires_at < now() "
            "ORDER BY expires_at, id LIMIT %s"
            ") DELETE FROM web_sessions WHERE id IN (SELECT id FROM expired)",
            (PASS_BATCH,),
        )
        return cur.rowcount


def _prune_schedule_fire_log_blocking(pool: ConnectionPool) -> int:
    """Delete ``schedule_fire_log`` claims older than the retention window.

    The table is the at-most-once claim ledger for schedule catch-up
    (``schedules/catchup.py``): the catch-up baseline is MAX(slot_fire_at) over
    the remaining rows. The newest claim PER SCHEDULE is always kept, so a
    sparse-cron schedule whose whole log is older than the window never
    regresses its baseline to ``created_at`` — a regressed baseline would let
    ``catch_up`` refire a slot the schedule already claimed. One bounded
    DELETE per pass: a backlog (long gateway outage, many schedules) drains
    over successive passes instead of one long transaction.
    """
    cutoff = datetime.now(UTC) - timedelta(days=settings.daemon.schedule_fire_log_retention_days)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM schedule_fire_log
            WHERE id IN (
                SELECT f.id
                FROM schedule_fire_log f
                WHERE f.slot_fire_at < %s
                  AND f.id NOT IN (
                    SELECT DISTINCT ON (schedule_id) id
                    FROM schedule_fire_log
                    ORDER BY schedule_id, slot_fire_at DESC
                  )
                ORDER BY f.id
                LIMIT %s
            )
            """,
            (cutoff, _FIRE_LOG_PASS_BATCH),
        )
        return cur.rowcount


async def _slow_phases(pool: ConnectionPool) -> tuple[int, int, int]:
    """The cadence-gated phases that came due: (pruned fire-log rows, torn
    lifecycle pointers found, absent-machine fences settled)."""
    pruned = torn = settled = 0
    if await asyncio.to_thread(
        cadence.claim_due,
        pool,
        cadence.FIRE_LOG_PRUNE,
        settings.daemon.schedule_fire_log_cleanup_interval_seconds,
    ):
        pruned = await asyncio.to_thread(_prune_schedule_fire_log_blocking, pool)
    if await asyncio.to_thread(
        cadence.claim_due, pool, cadence.TORN_POINTER_SCAN, cadence.HOURLY_S
    ):
        torn = await asyncio.to_thread(_scan_torn_lifecycle_pointers_blocking, pool)
    if await asyncio.to_thread(
        cadence.claim_due, pool, cadence.ABSENT_FENCE_SETTLE, cadence.HOURLY_S
    ):
        settled = len(await asyncio.to_thread(settle_absent_machine_fences, pool, batch=PASS_BATCH))
    return pruned, torn, settled


async def sweep_round(pool: ConnectionPool, progress: LoopProgress) -> None:
    """One pass over every database-only phase, beating `progress` between them."""
    reminded = await asyncio.to_thread(remind_expiring_impersonations, pool)
    progress.beat()
    impersonations = await asyncio.to_thread(reap_impersonations, pool)
    await asyncio.to_thread(alert_stuck_event_logs, pool)
    progress.beat()
    pages = await asyncio.to_thread(_reap_expired_pages_blocking, pool)
    progress.beat()
    sessions = await asyncio.to_thread(_reap_expired_web_sessions_blocking, pool)
    progress.beat()
    notices = await asyncio.to_thread(_reap_expired_notices_blocking, pool)
    for agent_id, nid in notices:
        with suppress(Exception):
            await lifecycle.publish_notice_resolved(EventBus.from_settings(), agent_id, nid)
    progress.beat()
    pruned_fire_log, torn_pointers, absent_fences = await _slow_phases(pool)
    if (
        pages
        or sessions
        or impersonations
        or notices
        or reminded
        or pruned_fire_log
        or torn_pointers
        or absent_fences
    ):
        _log.info(
            "[ttl-reaper] reclaimed %d page(s), %d web session(s), %d impersonation(s), "
            "%d notice(s); reminded %d impersonation lease(s); "
            "pruned %d schedule fire-log row(s); found %d torn lifecycle pointer(s); "
            "settled %d absent-machine lifecycle fence(s)",
            len(pages),
            sessions,
            impersonations,
            len(notices),
            reminded,
            pruned_fire_log,
            torn_pointers,
            absent_fences,
        )


async def sweep_loop(pool: ConnectionPool, progress: LoopProgress) -> None:
    """The database-only sweep as a resident sequential loop."""

    async def one_round() -> None:
        await sweep_round(pool, progress)

    await round_loop.run_rounds(
        "sweep", progress, settings.daemon.ttl_reaper_poll_interval_seconds, one_round
    )
