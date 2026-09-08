"""Gateway TTL reaper — enforce serve() page and persistent-shell deadlines.

The user ruling (2026-08-25) gives pages a hard lifetime (default 24h,
env-configurable via ``AVA_PAGE_DEFAULT_TTL_SECONDS``); the 2026-08-27 ruling
makes a TTL **mandatory** for every persistent shell session created via
``ava.shell.sessions.new(ttl=)`` / ``run_background(ttl=)`` (the
idle-shell-reminder daemon is gone; TTL is the only reclamation mechanism).
This loop is the enforcer, scanning
``agent_pages.expires_at``, ``agent_shell_ttls.expires_at``, and
``web_sessions.expires_at`` for rows past their deadline, plus unfinished
``work_failed_events`` older than their delivery grace window:

- **Pages** — the row is terminalized with ``expired_at`` (the reverse proxy
  then answers the page's link with the friendly "page expired" notice), a
  ``PageClosed`` event is published so the frontend drops the entry, and the
  page-server daemon stops the page session when its reconcile sees the row
  leave the open set (the daemon's existing two-layer teardown).
- **Shells** — a ``shell_kill`` op is dispatched to the owning agent's machine;
  the tracking row is removed once the session is killed or found already
  gone. A row whose machine is unreachable is left for the next pass. The
  owner's ``ava.shell.sessions.renew()`` extends a live session's deadline
  before it passes; each kill dispatch re-checks the row is still expired
  first (renewal's own ``expires_at > now()`` guard makes the pair airtight),
  so a renewed session is never killed.
- **Browser sessions** — expired rows are deleted in the gateway's periodic
  pass, so cleanup does not depend on the next login.
- **Work failures** — a gateway crash after recording an event but before
  finishing its route is retried through the original author/delegator/task
  fallback chain.

Owners are notified (inbound, source ``"system"``) only when the agent is
running or idling — a terminated agent's page expiring is exactly the cleanup
the TTL exists for, and must not resurrect it. Shell reclamations notify only
when the reap interrupted a running job (the runner reports whether the
session carried live processes at kill time); an empty shell's reaping is
silent, and an already-absent session never notifies. An interruption notice
states when the TTL expired and how long it was.

The pass is fail-open by design (never raises out of the loop): a DB or
dispatch failure logs and retries next interval, and every reclaim is a CAS
that loses cleanly to ``close()`` / termination.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg_pool import ConnectionPool

from gateway.routers import work_failed as work_failed_router
from ops import cluster_rpc, ops_lifecycle
from shared import telemetry
from shared.config import cluster_tz, settings
from shared.db import insert_inbound_message, publish_inbound_wake
from shared.db_transaction import write_transaction
from shared.impersonation_maintenance import reap_impersonations, remind_expiring_impersonations
from shared.inbound_provenance import InboundProvenance
from shared.live_announce import publish_agent_updated_sync
from shared.live_events import PageClosed
from shared.redis_client import publish_best_effort_sync

_log = logging.getLogger(__name__)

# Batch ceiling per pass: the tables are small by design; the cap keeps a
# backlog (e.g. after a long gateway outage) from turning one pass into a
# multi-minute transaction.
_PASS_BATCH = 200
# Per-op dispatch budget: a reachable runner answers a shell_kill in
# milliseconds; an unreachable one fails the connect within this bound.
_SHELL_KILL_TIMEOUT_S = 5.0

# The only agent states that can act on a reclamation notice. Terminated
# agents must NOT be resurrected by an expiry notification.
_NOTIFIABLE_STATUSES = ("running", "idling")


@dataclass
class TtlReaper:
    """Owned background task plus the event that drains it before shutdown."""

    task: asyncio.Task[None]
    stop: asyncio.Event


def _agent_machine(pool: ConnectionPool, agent_id: int) -> str | None:
    """The agent's registered home machine, or None when unusable."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    machine = row[0] if row is not None else None
    return str(machine) if machine and machine != "unknown" else None


def _notify_owner(
    conn: psycopg.Connection,
    agent_id: int,
    content: str,
    *,
    source: str = "system",
) -> None:
    """Insert a system-sourced inbound for a live owner; never resurrects.

    Skipped for terminated/restarting agents — a reclamation
    notice is informational and must not wake a dead agent back up.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None or row[0] not in _NOTIFIABLE_STATUSES:
        return
    inbound_id = insert_inbound_message(
        conn,
        agent_id,
        content,
        source=source,
        provenance=InboundProvenance(source_verified_by=None, source_transport="ops"),
    )
    with suppress(Exception):
        publish_inbound_wake(agent_id, str(inbound_id))


def _reap_expired_notices_blocking(pool: ConnectionPool) -> list[tuple[int, int]]:
    """Auto-resolve notices whose expire_at deadline has elapsed; return (agent_id, notice_id)."""
    with write_transaction(pool) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, agent_id, title, require_response FROM agent_notices "
                "WHERE expire_at <= now() AND resolved_at IS NULL "
                "ORDER BY expire_at, id LIMIT %s "
                "FOR UPDATE SKIP LOCKED",
                (_PASS_BATCH,),
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
            _notify_owner(
                conn,
                agent_id,
                f'Re: "{title}"\n\n[This notice has expired.]',
                source="system:notice-expire",
            )
            reaped.append((agent_id, nid))
        for aid in updated_agents:
            with suppress(Exception):
                publish_agent_updated_sync(conn, aid)
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
                (_PASS_BATCH,),
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
                _notify_owner(
                    conn,
                    agent_id,
                    f"Page {name!r} (agent {agent_id}) was reclaimed after its TTL "
                    "expired. Stop the page's HTTP server to release its port; "
                    "re-show with ava.ui.show() to republish.",
                )
            else:
                _notify_owner(
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
            (_PASS_BATCH,),
        )
        return cur.rowcount


def _expired_shell_rows_blocking(pool: ConnectionPool) -> list[tuple[int, int, datetime, datetime]]:
    """TTL-expired shell tracking rows, oldest deadline first.

    A row whose (agent, session) pair still carries a live watcher registry
    entry (``agent_watchers.status IN ('running', 'rebuilt')``) is skipped in
    the same SQL: a watcher session owns its lifecycle through the registry
    (its own deadline + the boot reconcile), never through a shell TTL. The
    NOT EXISTS guard makes that atomic — no TOCTOU window between a registry
    check and the kill. Schedule sessions never carry rows at all (they have
    no agent id; the ScheduleManager reaps them — see
    ``gateway/schedule_manager._launch``).

    Each row carries ``expires_at`` and ``created_at`` so the interruption
    notice can state when the TTL expired and how long it was (the duration
    is ``expires_at - created_at``; no extra column needed)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT t.agent_id, t.session_id, t.expires_at, t.created_at "
            "FROM agent_shell_ttls t "
            "WHERE t.expires_at <= now() "
            "AND NOT EXISTS ("
            "SELECT 1 FROM agent_watchers w "
            "WHERE w.agent_id = t.agent_id AND w.session_id = t.session_id "
            "AND w.status IN ('running', 'rebuilt')"
            ") "
            "ORDER BY t.agent_id, t.session_id LIMIT %s",
            (_PASS_BATCH,),
        )
        return [(row[0], row[1], row[2], row[3]) for row in cur.fetchall()]


def _claim_shell_row_still_expired(pool: ConnectionPool, agent_id: int, session_id: int) -> bool:
    """Re-verify one expired shell row just before dispatching its kill.

    The expired-row select and the kill dispatch are separated by a machine
    lookup, and the owner's ``sessions.renew()`` may land in between. A no-op
    UPDATE claims the row only while it is STILL expired (rowcount 1); a
    renewal in the gap makes the claim fail and the pass skips the kill —
    the row is no longer expired and would not be selected next pass either.
    Combined with renewal's ``expires_at > now()`` write guard the pair is
    airtight: a successful renewal always precedes any expired-row select, so
    the only losing renewal is one racing its own deadline.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_shell_ttls SET expires_at = expires_at "
            "WHERE agent_id = %s AND session_id = %s AND expires_at <= now()",
            (agent_id, session_id),
        )
        return cur.rowcount == 1


def _human_ttl(seconds: float) -> str:
    """A TTL duration as a compact human string: 1h, 30m, 90s."""
    seconds = max(0, int(seconds))
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _wall_clock(dt: datetime) -> str:
    """Local wall-clock for the notice: HH:MM, or MM-DD HH:MM when the moment
    is not on the cluster's today (a TTL can cross midnight).

    Renders in the cluster timezone; ``None`` falls back to the host zone
    (``dt.astimezone(None)``) — the shared/config contract that ``None`` is
    the host-zone fallback signal."""
    tz = cluster_tz()
    local = dt.astimezone(tz)
    stamp = local.strftime("%H:%M")
    if local.date() != datetime.now(tz).date():
        stamp = f"{local.strftime('%m-%d')} {stamp}"
    return stamp


def _delete_shell_row_blocking(
    pool: ConnectionPool,
    agent_id: int,
    session_id: int,
    *,
    interrupted: bool,
    name: str | None = None,
    expires_at: datetime | None = None,
    created_at: datetime | None = None,
) -> None:
    """Drop a reclaimed shell's tracking row; notify its live owner only when
    the reclamation interrupted a running job (an empty shell's reaping is
    silent — user ruling 2026-08-27). The notice states when the TTL expired
    and how long it was (``expires_at`` / ``created_at`` from the row)."""
    with write_transaction(pool) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agent_shell_ttls WHERE agent_id = %s AND session_id = %s",
                (agent_id, session_id),
            )
        if interrupted:
            label = (
                f"Shell session {name!r} (id {session_id}, agent {agent_id})"
                if name
                else f"Shell session {session_id} (agent {agent_id})"
            )
            detail = ""
            if expires_at is not None:
                ttl = (
                    f", TTL {_human_ttl((expires_at - created_at).total_seconds())}"
                    if created_at is not None
                    else ""
                )
                detail = f" at {_wall_clock(expires_at)}{ttl}"
            _notify_owner(
                conn,
                agent_id,
                f"{label} was reclaimed after its TTL expired{detail}, interrupting a running task.",
            )


async def _reap_expired_shells(pool: ConnectionPool) -> list[tuple[int, int]]:
    """Kill TTL-expired shell sessions on their home machines.

    The row is deleted only on a definitive verdict (killed / absent); an
    unreachable machine or a version-skewed runner leaves it for the next
    pass — deleting the row would orphan the live session. All DB work runs
    via to_thread: the gateway event loop never blocks on psycopg.
    """
    rows = await asyncio.to_thread(_expired_shell_rows_blocking, pool)
    reaped: list[tuple[int, int]] = []
    for agent_id, session_id, expires_at, created_at in rows:
        if not await asyncio.to_thread(_claim_shell_row_still_expired, pool, agent_id, session_id):
            # Renewed between the select and this pass — the deadline moved
            # forward, so the session is no longer reaper business.
            _log.info(
                "[ttl-reaper] shell %s of agent %s was renewed — skipping",
                session_id,
                agent_id,
            )
            continue
        machine = await asyncio.to_thread(_agent_machine, pool, agent_id)
        if machine is None:
            _log.warning(
                "[ttl-reaper] shell %s of agent %s has unknown machine — deferring",
                session_id,
                agent_id,
            )
            continue
        try:
            result = await cluster_rpc.dispatch_to_machine(
                machine,
                "shell_kill",
                {"agent_id": agent_id, "session_id": session_id},
                timeout_s=_SHELL_KILL_TIMEOUT_S,
            )
        except (cluster_rpc.ClusterOpUnreachable, cluster_rpc.ClusterOpFailed) as exc:
            _log.warning(
                "[ttl-reaper] shell_kill for agent %s session %s deferred: %r",
                agent_id,
                session_id,
                exc,
            )
            continue
        mode = result.get("mode")
        if mode not in ("killed", "absent"):
            _log.warning(
                "[ttl-reaper] shell_kill for agent %s session %s returned %r",
                agent_id,
                session_id,
                result,
            )
            continue
        # Notify only when the reap cut short a running job. A missing
        # `interrupted` field means a pre-policy runner — default True so a
        # version-skewed fleet keeps the old notify-always behavior instead of
        # silently swallowing a legit interruption notice. Absent sessions
        # never notify (nothing was interrupted).
        interrupted = mode == "killed" and bool(result.get("interrupted", True))
        await asyncio.to_thread(
            _delete_shell_row_blocking,
            pool,
            agent_id,
            session_id,
            interrupted=interrupted,
            name=result.get("name"),
            expires_at=expires_at,
            created_at=created_at,
        )
        telemetry.emit(
            "log",
            "shell_ttl_expired",
            level="info",
            agent_id=agent_id,
            attributes={
                "agent_id": agent_id,
                "session_id": session_id,
                "mode": mode,
                "interrupted": interrupted,
            },
        )
        reaped.append((agent_id, session_id))
    return reaped


async def _reaper_loop(pool: ConnectionPool, stop: asyncio.Event) -> None:
    """Reclaim once at startup, then on the configured interval."""
    while not stop.is_set():
        try:
            reminded = await asyncio.to_thread(remind_expiring_impersonations, pool)
            impersonations = await asyncio.to_thread(reap_impersonations, pool)
            pages = await asyncio.to_thread(_reap_expired_pages_blocking, pool)
            shells = await _reap_expired_shells(pool)
            sessions = await asyncio.to_thread(_reap_expired_web_sessions_blocking, pool)
            notices = await asyncio.to_thread(_reap_expired_notices_blocking, pool)
            for agent_id, nid in notices:
                with suppress(Exception):
                    await ops_lifecycle.publish_notice_resolved(agent_id, nid)
            failures = await work_failed_router.reconcile_stale_work_failures(pool)
            if pages or shells or sessions or impersonations or notices or failures or reminded:
                _log.info(
                    "[ttl-reaper] reclaimed %d page(s), %d shell(s), %d web session(s), %d impersonation(s), %d notice(s); "
                    "completed %d stale work failure(s); reminded %d impersonation lease(s)",
                    len(pages),
                    len(shells),
                    sessions,
                    impersonations,
                    len(notices),
                    failures,
                    reminded,
                )
        except Exception:
            _log.warning("[ttl-reaper] pass failed", exc_info=True)
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=settings.daemon.ttl_reaper_poll_interval_seconds
            )
        except TimeoutError:
            continue


def start_ttl_reaper(db_pool: ConnectionPool) -> TtlReaper:
    """Start the gateway TTL reaper loop."""
    stop = asyncio.Event()
    task = asyncio.create_task(_reaper_loop(db_pool, stop))
    return TtlReaper(task=task, stop=stop)


async def stop_ttl_reaper(reaper: TtlReaper) -> None:
    """Drain a bounded in-flight pass before the gateway closes its pool."""
    reaper.stop.set()
    with suppress(asyncio.CancelledError):
        await reaper.task
