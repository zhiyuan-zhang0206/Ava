"""Watcher-side TTL reclamation — the watcher half of `gateway.ttl_reaper`.

A watcher session's shell TTL IS the watcher's target deadline (user ruling
2026-09-14, task #3411): launch = created + timeout, cron = end, at = moment
+ grace — one derivation (`shared.watcher.session_deadline`) for the spawn
write path, the boot reconcile, and this reclaim side. Everything here is
that rule applied to the registry:

- `watcher_deadline_of` reads the deadline off an expired-row record;
- `heal_legacy_ttl` re-aligns a row spawned before the unified write path
  (its recorded TTL is a placeholder, not the target) instead of reclaiming
  a session still living its true window;
- `mark_reaped_live_owner` / `mark_reaped_and_notify_if_owner_still_terminated`
  terminalize the registry row once a reclaim is definitive — past the
  deadline a watcher is never rebuilt, and the owner-appropriate notice is
  the only difference between the two;
- `reap_terminated_owner_watchers` is the #2617 path for owners that never
  come back to reconcile: their watcher sessions are killed and the rows
  terminalized with a reclamation notice (delivered on the owner's next
  resurrect, never resurrecting it itself).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from psycopg_pool import ConnectionPool

from ops import cluster_rpc
from shared import telemetry
from shared.db import insert_inbound_message
from shared.db_transaction import write_transaction
from shared.inbound_provenance import InboundProvenance
from shared.watcher import session_deadline

_log = logging.getLogger(__name__)


def watcher_deadline_of(row: dict[str, Any]) -> datetime | None:
    """The watcher deadline carried by one expired-row record, or None for a
    plain shell row (no watcher facts joined)."""
    kind = row["watcher_kind"]
    if kind is None:
        return None
    return session_deadline(
        kind,
        created_at=row["watcher_created_at"],
        timeout_secs=row["watcher_timeout_secs"],
        fires_at=row["watcher_fires_at"],
        cron_end_at=row["watcher_cron_end_at"],
    )


def heal_legacy_ttl(
    pool: ConnectionPool, agent_id: int, session_id: int, deadline: datetime
) -> bool:
    """Re-align a legacy watcher session's TTL row to its true deadline.

    Only rows spawned before the unified write path (user ruling 2026-09-14,
    task #3411) carry a placeholder TTL (launch = min(timeout, 24h); cron and
    at = 24h) that is NOT the watcher's target — reclaiming such a session at
    the placeholder would cut short a schedule still living its intended
    window (and permanently, once the row is marked reaped). Instead the
    recorded deadline is rewritten to the true one, derived by
    ``shared.watcher.session_deadline`` — the same source the spawn path
    writes, so reclaim / reconcile / display read one value again.

    The UPDATE is a CAS on ``expires_at <= clock_timestamp()``: a concurrent
    heal or reclaim that already moved the row makes this a no-op — the heal
    is idempotent by construction, and a renewed row is never touched.
    Returns True when the row was rewritten."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_shell_ttls SET expires_at = %s "
            "WHERE agent_id = %s AND session_id = %s AND expires_at <= clock_timestamp()",
            (deadline, agent_id, session_id),
        )
        return bool(cur.rowcount == 1)


def mark_reaped_live_owner(pool: ConnectionPool, agent_id: int, session_id: int) -> str | None:
    """Mark a still-``running`` watcher row ``reaped`` after its session was
    reclaimed at (or past) its deadline, when its owner is a live agent.

    One rule with the boot reconcile's deadline check (task #3411): past the
    deadline a watcher is reclaimed, never rebuilt — ``reaped`` is the status
    that says so, and no later boot will reconsider the row. Only a
    still-``running`` row of a running/idling owner is touched: a concurrent
    reconcile may have terminalized it already (that verdict stands), and a
    crash-terminated owner's row is left ``running`` on purpose — the
    #2589/#1938 discipline, never terminalize what may come back; its own
    resurrect + boot reconcile reaches the same deadline verdict. Returns
    the watcher's name when the row was marked, None otherwise."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_watchers w SET status = 'reaped', updated_at = now() "
            "FROM agents_meta m "
            "WHERE w.agent_id = %s AND w.session_id = %s AND w.status = 'running' "
            "AND m.id = w.agent_id AND m.status IN ('running', 'idling') "
            "RETURNING w.name",
            (agent_id, session_id),
        )
        row = cur.fetchone()
        return None if row is None else str(row[0])


def terminated_owner_watcher_rows(
    pool: ConnectionPool, *, batch: int
) -> list[tuple[int, int, str]]:
    """Watcher rows whose owner agent is terminated for good, newest first.

    Only ``status='running'`` rows carry live sessions; the other statuses
    are terminal history. Owners terminated by crash recovery
    (``reaper`` / ``launch-confirm``) are KEPT — they are auto-resurrect-
    eligible and their own cron wakes are a revival channel; killing them
    would race the crash-resurrect controller (the #2589 / #1938 lesson:
    never reap what may come back). Every other terminated source (user /
    exit / integrity / legacy NULL) is permanent, so its watchers are dead
    weight — exactly the task #2617 leak. The machine is carried in the
    same SELECT so the kill dispatch never re-reads the owner.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT w.agent_id, w.session_id, m.machine
            FROM agent_watchers w
            JOIN agents_meta m ON m.id = w.agent_id
            WHERE m.status = 'terminated'
              AND COALESCE(m.termination_source, '') NOT IN ('reaper', 'launch-confirm')
              AND w.status = 'running'
            ORDER BY w.created_at DESC, w.session_id DESC
            LIMIT %s
            """,
            (batch,),
        )
        return [(int(r[0]), int(r[1]), r[2]) for r in cur.fetchall()]


def mark_reaped_and_notify_if_owner_still_terminated(
    pool: ConnectionPool, agent_id: int, session_id: int
) -> str | None:
    """Terminalize a reclaimed watcher row and queue its reclamation notice,
    re-verifying its owner in the SAME statement (the #2589 atomic-guard
    discipline).

    The owner may have been resurrected between the scan and the kill — then
    the row must stay ``running`` so the resurrected agent's boot reconcile
    rebuilds the schedule from it. When the row IS marked, the #2060 notice
    (user ruling 2026-09-10: reaped is terminal, the schedule never
    auto-restores — the owner must be told it was reclaimed so it can
    re-register) is inserted in the SAME transaction, so a crash between the
    mark and the notice cannot lose the notice.

    The owner is terminated at mark time, so the notice is inserted WITHOUT
    the live-owner gate of ``_notify_owner``: it stays a pending inbound and
    delivers on the agent's next resurrect through any channel, without
    resurrecting it (a reclamation notice never resurrects — the wake
    publish reaches no listener of a terminated agent, and the pending row
    is claimed when the agent comes back). Returns the watcher's name when
    the row was marked, None otherwise.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE agent_watchers SET status = 'reaped', updated_at = now()
            WHERE agent_id = %s AND session_id = %s AND status = 'running'
              AND EXISTS (
                  SELECT 1 FROM agents_meta m
                  WHERE m.id = agent_watchers.agent_id
                    AND m.status = 'terminated'
                    AND COALESCE(m.termination_source, '') NOT IN ('reaper', 'launch-confirm')
              )
            RETURNING name
            """,
            (agent_id, session_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        name = row[0]
        insert_inbound_message(
            conn,
            agent_id,
            (
                f"Watcher schedule {name!r} (agent {agent_id}) was reclaimed "
                "after its TTL expired. Re-register it with ava.watcher.cron() "
                "if it is still needed."
            ),
            source="system",
            provenance=InboundProvenance(source_verified_by=None, source_transport="ops"),
        )
        return name


async def reap_terminated_owner_watchers(
    pool: ConnectionPool, *, timeout_s: float, batch: int, stop: asyncio.Event | None = None
) -> list[tuple[int, int]]:
    """Kill watcher sessions whose owner agent is terminated for good.

    Mirrors ``_reap_expired_shells`` discipline: the row is terminalized
    only on a definitive kill verdict (killed / absent); an unreachable
    machine or a failed op leaves the row for the next pass — marking it
    first would orphan the live session. The post-kill mark re-checks the
    owner in SQL, so a mid-flight resurrect leaves the row ``running`` for
    the agent's own reconcile to rebuild (never a silent loss). A definitive
    mark also queues the #2060 reclamation notice for the owner — reaped is
    terminal, so the owner must be told to re-register if it still needs the
    schedule (the notice delivers on the owner's next resurrect; it never
    resurrects the owner itself).

    A set ``stop`` defers the not-yet-started rows to the next pass (same
    shutdown contract as ``_reap_expired_shells``): shutdown waits for the
    in-flight dispatch, never for the rest of the batch.
    """
    rows = await asyncio.to_thread(terminated_owner_watcher_rows, pool, batch=batch)
    reaped: list[tuple[int, int]] = []
    for agent_id, session_id, machine in rows:
        if stop is not None and stop.is_set():
            break
        try:
            result = await cluster_rpc.dispatch_to_machine(
                machine,
                "shell_kill",
                {"agent_id": agent_id, "session_id": session_id},
                timeout_s=timeout_s,
            )
        except (cluster_rpc.ClusterOpUnreachable, cluster_rpc.ClusterOpFailed) as exc:
            _log.warning(
                "[ttl-reaper] terminated-owner watcher kill for agent %s session %s deferred: %r",
                agent_id,
                session_id,
                exc,
            )
            continue
        mode = result.get("mode")
        if mode not in ("killed", "absent"):
            _log.warning(
                "[ttl-reaper] terminated-owner watcher kill for agent %s session %s returned %r",
                agent_id,
                session_id,
                result,
            )
            continue
        marked = await asyncio.to_thread(
            mark_reaped_and_notify_if_owner_still_terminated, pool, agent_id, session_id
        )
        if not marked:
            _log.info(
                "[ttl-reaper] watcher %s of agent %s — owner no longer terminated; "
                "leaving the row for the agent's boot reconcile",
                session_id,
                agent_id,
            )
            continue
        telemetry.emit(
            "log",
            "watcher_reaped",
            level="info",
            agent_id=agent_id,
            attributes={"agent_id": agent_id, "session_id": session_id, "mode": mode},
        )
        reaped.append((agent_id, session_id))
    return reaped
