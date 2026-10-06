"""Reclaim TTL-expired persistent shell sessions on their home machines.

The 2026-08-27 ruling makes a TTL mandatory for every persistent shell session
created via ``ava.shell.sessions.new(ttl=)`` / ``run_background(ttl=)``; the TTL
is the only reclamation mechanism. Every session — a plain shell or a watcher
(``ava.watcher.at/cron/launch``), which is just a shell session running a
generated script with no registry or deadline of its own
(docs/decisions/2026-09-27-watchers-are-never-restarted.md) — carries one
``agent_shell_ttls`` row, and every expired row is reclaimed the same way: a
``shell_kill`` op to the owning agent's machine, then the row is deleted.

The row is deleted only on a definitive verdict (killed / absent /
machine_absent); an unreachable machine or a version-skewed runner leaves it for
the next round — deleting the row would orphan the live session. A machine
absent from the machines registry is definitive too (nothing on it can be dialed
again under that name — task #4143): the row terminalizes with the
``machine_absent`` verdict instead of deferring forever.

One round groups the expired rows by home machine: machines are reclaimed
concurrently, the rows of one machine one after another, so a slow machine holds
up only its own rows. Each dispatch runs under a deadline sized from the RPC
client's own budget.

The owner's ``ava.shell.sessions.renew()`` extends a live session's deadline
before it passes; each kill dispatch re-checks the row is still expired first
(renewal's own ``expires_at > clock_timestamp()`` guard makes the pair airtight),
so a renewed session is never killed.

Owners are notified (inbound, source ``"system"``) only when the reap interrupted
a running job (the runner reports whether the session carried live processes at
kill time); an empty shell's reaping is silent, and an already-absent session
never notifies. A watcher session always carries a running job, so reclaiming
one notifies every time. An interruption notice states when the TTL expired and
how long it was.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents import ShellKillMode
from base.clock import Clock
from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.db.transaction import write_transaction
from base.events.live.bus import EventBus
from ops import cluster_rpc
from services.upkeep.ttl_reaper.owner_notice import PASS_BATCH, notify_owner

_log = logging.getLogger(__name__)

# Per-attempt dispatch budget: a reachable runner answers a shell_kill in
# milliseconds; an unreachable one fails the connect within this bound.
_SHELL_KILL_TIMEOUT_S = 5.0
# Machines reclaimed at once within a round.
_MACHINE_CONCURRENCY = 8


def dispatch_deadline_s() -> float:
    """Overall deadline for one shell_kill dispatch: the RPC client's worst case
    at the per-attempt budget, so it only cuts a dispatch the client's own
    timeout and retry budgets did not."""
    return cluster_rpc.worst_case_dispatch_seconds(_SHELL_KILL_TIMEOUT_S)


def _expired_shell_rows_blocking(pool: ConnectionPool) -> list[dict[str, Any]]:
    """TTL-expired shell tracking rows with their owner's home machine, oldest
    deadline first.

    Schedule sessions never carry rows at all (they have no agent id; the
    ScheduleManager reaps them — see ``services/wake/schedule_manager/manager.py``
    ``_launch``).

    Each row carries ``expires_at`` and ``created_at`` so the interruption
    notice can state when the TTL expired and how long it was (the duration
    is ``expires_at - created_at``; no extra column needed)."""
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT t.agent_id, t.session_id, t.expires_at, t.created_at, m.machine "
            "FROM agent_shell_ttls t LEFT JOIN agents_meta m ON m.id = t.agent_id "
            "WHERE t.expires_at <= clock_timestamp() "
            "ORDER BY t.agent_id, t.session_id LIMIT %s",
            (PASS_BATCH,),
        )
        return [dict(row) for row in cur.fetchall()]


def _claim_still_expired(cur: psycopg.Cursor[Any], agent_id: int, session_id: int) -> bool:
    """The claim UPDATE; True only while the row is STILL expired.

    The guard compares against ``clock_timestamp()`` — the statement time,
    evaluated after the row lock is acquired — never ``now()`` (transaction
    start). The claim transaction can begin long before the deadline while it
    waits on a busy row; with ``now()`` it would skip a row that expired after
    its transaction started, and it would not serialize correctly against a
    renewal whose own guard also evaluates at statement time (issue #2053).
    """
    cur.execute(
        "UPDATE agent_shell_ttls SET expires_at = expires_at "
        "WHERE agent_id = %s AND session_id = %s AND expires_at <= clock_timestamp()",
        (agent_id, session_id),
    )
    return cur.rowcount == 1


def _claim_shell_row_still_expired(pool: ConnectionPool, agent_id: int, session_id: int) -> bool:
    """Re-verify one expired shell row just before dispatching its kill.

    The expired-row select and the kill dispatch are separated by the other
    rows of the machine, and the owner's ``sessions.renew()`` may land in
    between. A no-op UPDATE claims the row only while it is STILL expired
    (rowcount 1); a renewal in the gap makes the claim fail and the round skips
    the kill — the row is no longer expired and would not be selected next round
    either. Combined with renewal's ``expires_at > clock_timestamp()`` write
    guard the pair is airtight: whichever of the claim and the renewal acquires
    the row lock first wins, and the loser re-evaluates at its own statement
    time.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        return _claim_still_expired(cur, agent_id, session_id)


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
    (``dt.astimezone(None)``) — the base/config contract that ``None`` is
    the host-zone fallback signal."""
    tz = Clock.from_settings().zone()
    local = dt.astimezone(tz)
    stamp = local.strftime("%H:%M")
    if local.date() != datetime.now(tz).date():
        stamp = f"{local.strftime('%m-%d')} {stamp}"
    return stamp


def delete_shell_row(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
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
    and how long it was (``expires_at`` / ``created_at`` from the row).

    A watcher session (just a shell session running a generated script) gets
    this exact same notice — it always carries a running job, so the reclaim
    always interrupts one."""
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
            notify_owner(
                conn,
                db,
                bus,
                agent_id,
                f"{label} was reclaimed after its TTL expired{detail}, interrupting a running task.",
            )


class _MachineAbsent:
    """Local registry evidence, never a verdict accepted from a runner payload."""


async def _dispatch_shell_kill(
    db: Database, machine: str, agent_id: int, session_id: int
) -> dict[str, Any] | _MachineAbsent | None:
    """One ``shell_kill`` dispatch; None means "defer to the next round".

    A machine absent from the machines registry (task #4143) cannot be dialed:
    the row terminalizes with the distinct ``machine_absent`` verdict — the
    live-host ``absent`` means the host answered that the session already
    ended, while no host exists here to answer at all. Deferring would retry
    the row forever. An unreachable machine, a failed op or a dispatch that
    outlives its deadline defers: the session may still live.
    """
    try:
        async with asyncio.timeout(dispatch_deadline_s()):
            return await cluster_rpc.dispatch_to_machine(
                db,
                machine,
                "shell_kill",
                {"agent_id": agent_id, "session_id": session_id},
                timeout_s=_SHELL_KILL_TIMEOUT_S,
            )
    except cluster_rpc.ClusterOpTargetAbsent:
        _log.info(
            "[ttl-reaper] shell %s of agent %s: machine %r is absent from the "
            "registry — terminalizing with the machine_absent verdict",
            session_id,
            agent_id,
            machine,
        )
        return _MachineAbsent()
    except (cluster_rpc.ClusterOpUnreachable, cluster_rpc.ClusterOpFailed, TimeoutError) as exc:
        _log.warning(
            "[ttl-reaper] shell_kill for agent %s session %s deferred: %r",
            agent_id,
            session_id,
            exc,
        )
        return None


async def _reclaim_row(
    pool: ConnectionPool, db: Database, bus: EventBus, machine: str, row: dict[str, Any]
) -> tuple[int, int] | None:
    """Reclaim one expired shell on `machine`; the (agent, session) when its row
    was settled, None when the row is left for the next round."""
    agent_id = row["agent_id"]
    session_id = row["session_id"]
    if not await asyncio.to_thread(_claim_shell_row_still_expired, pool, agent_id, session_id):
        # Renewed between the select and this dispatch — the deadline moved
        # forward, so the session is no longer reaper business.
        _log.info("[ttl-reaper] shell %s of agent %s was renewed — skipping", session_id, agent_id)
        return None
    result = await _dispatch_shell_kill(db, machine, agent_id, session_id)
    if result is None:
        return None
    if isinstance(result, _MachineAbsent):
        mode = None
    else:
        try:
            mode = ShellKillMode(result["mode"])
        except (KeyError, ValueError):
            _log.warning(
                "[ttl-reaper] shell_kill for agent %s session %s returned %r",
                agent_id,
                session_id,
                result,
            )
            return None
    # Notify only when the reap cut short a running job. A missing
    # `interrupted` field means a pre-policy runner — default True so a
    # version-skewed fleet keeps the old notify-always behavior instead of
    # silently swallowing a legit interruption notice. Absent sessions
    # never notify (nothing was interrupted).
    interrupted = (
        mode is ShellKillMode.KILLED
        and isinstance(result, dict)
        and bool(result.get("interrupted", True))
    )
    await asyncio.to_thread(
        functools.partial(
            delete_shell_row,
            pool,
            db,
            bus,
            agent_id,
            session_id,
            interrupted=interrupted,
            name=result.get("name") if isinstance(result, dict) else None,
            expires_at=row["expires_at"],
            created_at=row["created_at"],
        )
    )
    telemetry.emit(
        "log",
        "shell_ttl_expired",
        level="info",
        agent_id=agent_id,
        attributes={
            "agent_id": agent_id,
            "session_id": session_id,
            "mode": mode.value if mode is not None else "machine_absent",
            "interrupted": interrupted,
        },
    )
    return agent_id, session_id


async def reap_expired_shells(
    pool: ConnectionPool, db: Database, bus: EventBus, progress: LoopProgress
) -> list[tuple[int, int]]:
    """Kill every TTL-expired shell session on its home machine; return the
    (agent, session) pairs whose rows were settled.

    Machines run concurrently, the rows of one machine in order. A row whose
    owner has no usable home machine is left for the next round. All DB work
    runs via to_thread: the event loop never blocks on psycopg.
    """
    rows = await asyncio.to_thread(_expired_shell_rows_blocking, pool)
    by_machine: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        machine = row["machine"]
        if not machine or machine == "unknown":
            _log.warning(
                "[ttl-reaper] shell %s of agent %s has unknown machine — deferring",
                row["session_id"],
                row["agent_id"],
            )
            continue
        by_machine[machine].append(row)
    reaped: list[tuple[int, int]] = []

    async def reclaim_machine(machine: str, machine_rows: list[dict[str, Any]]) -> None:
        for row in machine_rows:
            settled = await _reclaim_row(pool, db, bus, machine, row)
            if settled is not None:
                reaped.append(settled)
            progress.beat()

    await round_loop.fan_out(
        [functools.partial(reclaim_machine, machine, rs) for machine, rs in by_machine.items()],
        concurrency=_MACHINE_CONCURRENCY,
        progress=progress,
    )
    return reaped
