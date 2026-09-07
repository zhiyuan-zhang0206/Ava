"""The watcher registry — the "should it exist?" half of the R1 watcher frame.

`ava.watcher.at/cron/launch` writes one row per spawned watcher session
(`agent_watchers`, Task #1021). The row outlives the session exactly when the
session was KILLED rather than ended: a watcher that exits cleanly deletes its
own row (the generated bootstrap's finally), so a surviving row with a missing
session means "this watcher should exist and does not". The agent's boot
reconcile (`ava.watcher.reconcile`) reads that and rebuilds cron watchers /
marks missed one-shots — the #1014 fix (4th recurrence: rollouts reaped
watcher sessions and nothing knew they should exist).

Liveness is the session itself — a watcher process IS its session — so the
registry deliberately holds no lease: a session gone means the watcher is gone,
and the reconcile is the only reader that needs to know.

Pure DB: no SDK imports, so the watcher child's bootstrap finally can call
`delete_watcher` without pulling the SDK into a short-lived child.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from psycopg.rows import dict_row

from shared.db import connect
from shared.db_transaction import write_transaction

logger = logging.getLogger(__name__)

# The statuses the reconcile transitions through. `running` is the spawn state;
# `rebuilt` marks a row whose watcher died and was re-spawned (history kept —
# the rebuild's NEW session has its own `running` row); `missed` marks a
# one-shot whose moment passed while its session was gone; and `reaped` retains
# an obsolete generation without letting a later boot rebuild it.
_WATCHER_STATUSES = ("running", "rebuilt", "missed", "reaped")

# The rebuild payload columns, per kind. The reconcile rebuilds a watcher by
# re-calling the SDK verb with exactly these arguments, so the row must carry
# everything the verb needs.
_KIND_PAYLOAD = {
    "at": ("message", "fires_at"),
    "cron": ("message", "cron_expr", "cron_timezone", "cron_end_at"),
    "launch": (),
}


_REGISTER_SQL = """
    INSERT INTO agent_watchers (
        session_id, agent_id, kind, name, message, fires_at,
        cron_expr, cron_timezone, cron_end_at, timeout_secs,
        template_version, generation
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (agent_id, session_id) DO NOTHING
"""


def cron_advisory_key(
    agent_id: int,
    cron_expr: str,
    cron_timezone: str,
    cron_end_at: Any,
) -> int:
    """The pg_advisory_xact_lock key serializing one (agent, cron schedule) pair.

    ONE 64-bit int: the agent id in the high 32 bits, a stable CRC32 of the
    schedule (expr, timezone, normalized end time) in the low 32 bits. A
    single int8 keeps the call an unambiguous `pg_advisory_xact_lock(int8)`
    — two ints would arrive as (int2|int4, int8) and match no overload (the
    CRC32 is unsigned and exceeds int4). Collisions only over-serialize (two
    schedules sharing a key wait on each other briefly) — never
    under-serialize. The normalized end time is UTC-aware, so the key is
    stable across processes and machines (Task #1825 N2).
    """
    import zlib

    end = cron_end_at.isoformat() if cron_end_at is not None else ""
    schedule = zlib.crc32(f"{cron_expr}\x00{cron_timezone}\x00{end}".encode()) & 0xFFFFFFFF
    return (agent_id << 32) | schedule


def register_cron_atomic(
    agent_id: int,
    session_id: int,
    *,
    name: str,
    message: str | None,
    cron_expr: str,
    cron_timezone: str,
    cron_end_at: Any,
    alive_provider: Callable[[], set[int] | None],
    exclude_session: int | None = None,
    template_version: int | None = None,
    generation: str | None = None,
) -> int | None:
    """Register one cron watcher row atomically — reuse a live duplicate or insert.

    ONE transaction on ONE connection: `pg_advisory_xact_lock` on the
    (agent, schedule) key, then a re-check for a live row with the same
    schedule, then the INSERT when none exists. Returns the REUSED session id
    when a live duplicate exists (the new row is NOT inserted and the
    transaction rolls back); returns None when the new row was inserted and
    committed. Two concurrent registrations of the same schedule serialize on
    the lock: the loser's re-check sees the winner's committed row and reuses
    it — the Task #1825 dedupe becomes atomic (N2).

    The lock is TRANSACTION-scoped, deliberately not session-level: normal
    processes dial the cluster's PgBouncer (`pool_mode = transaction`,
    `server_reset_query = DISCARD ALL`), which silently drops session-level
    advisory locks when the pooled backend returns to the pool — the #794
    BLOCK. One transaction pins one pooled backend, so lock + check + insert
    execute on the same backend and the lock lives exactly as long as the
    serialization needs it; on a direct connection the semantics are
    identical.

    `alive_provider` is CALLED INSIDE the lock (after pg_advisory_xact_lock),
    never before: the caller's session list is only meaningful once a
    concurrent winner's registration is committed and its session visible —
    a snapshot taken before the lock has a window where the winner's session
    does not yet exist and the re-check would miss it (QA nit, #794 delta2).
    It returns the fresh session-id set, or None when the session list is
    unavailable (no dedupe then — a duplicate is recoverable, a reused dead
    session would silently lose the schedule); it must not raise. A
    same-generation, same-schedule `running` row counts as a duplicate only
    when its session is in the set (a dead row is exactly what the boot
    reconcile is about to rebuild — it must not block a fresh registration).
    `exclude_session` skips one row — the stale-template rebuild must not
    dedupe against the live session it is replacing.
    """
    # `exclude_session` may be 0 — an agent's very first session
    # (session_index starts at 0) — so the sentinel must be an explicit None
    # check, never `or -1` (QA nit, #794 delta2).
    exclude = -1 if exclude_session is None else exclude_session
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ WRITE")
        cur.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (cron_advisory_key(agent_id, cron_expr, cron_timezone, cron_end_at),),
        )
        # Fresh liveness INSIDE the lock: the caller's session list is only
        # meaningful once any concurrent winner's registration is committed
        # and its session visible (QA nit, #794 delta2).
        alive = alive_provider()
        cur.execute(
            """
            SELECT session_id FROM agent_watchers
            WHERE agent_id = %s AND kind = 'cron' AND status = 'running'
              AND cron_expr = %s AND cron_timezone = %s
              AND cron_end_at IS NOT DISTINCT FROM %s
              AND generation IS NOT DISTINCT FROM %s
              AND session_id != %s
            ORDER BY created_at DESC, session_id DESC
            LIMIT 1
            """,
            (agent_id, cron_expr, cron_timezone, cron_end_at, generation, exclude),
        )
        row = cur.fetchone()
        if row is not None and alive is not None and row[0] in alive:
            return int(row[0])
        cur.execute(
            _REGISTER_SQL,
            (
                session_id,
                agent_id,
                "cron",
                name,
                message,
                None,
                cron_expr,
                cron_timezone,
                cron_end_at,
                None,
                template_version,
                generation,
            ),
        )
    return None


def register_watcher(
    agent_id: int,
    session_id: int,
    *,
    kind: str,
    name: str,
    message: str | None = None,
    fires_at: Any = None,
    cron_expr: str | None = None,
    cron_timezone: str | None = None,
    cron_end_at: Any = None,
    timeout_secs: float | None = None,
    template_version: int | None = None,
    generation: str | None = None,
) -> None:
    """Record a watcher session at spawn (`ava.watcher._spawn`).

    `kind` must be one of at/cron/launch and the row carries that kind's
    rebuild payload (see `_KIND_PAYLOAD`). `template_version` is the generated
    script's template generation (shared.watcher.TEMPLATE_VERSION); the boot
    reconcile rebuilds a live cron watcher whose row version is behind, so a
    template fix reaches sessions that were already running when it landed
    (issue #1330). `generation` identifies the PTY record that this desired
    row may restore. Fail-soft at the call site: a registry write must never
    break the watcher it is only observing.

    Cron registrations go through `register_cron_atomic` instead — the
    Task #1825 dedupe lives in the registration itself.
    """
    if kind not in _KIND_PAYLOAD:
        raise ValueError(f"unknown watcher kind {kind!r}")
    with write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            _REGISTER_SQL,
            (
                session_id,
                agent_id,
                kind,
                name,
                message,
                fires_at,
                cron_expr,
                cron_timezone,
                cron_end_at,
                timeout_secs,
                template_version,
                generation,
            ),
        )


def delete_watcher(agent_id: int, session_id: int) -> None:
    """Drop a watcher's registry row.

    Called from the watcher child's clean-exit finally (fired / ended /
    crashed-with-finally) and from `ava.shell.sessions.kill` (a deliberately
    killed watcher must not be rebuilt at the next boot). A missing row is a
    no-op.

    `session_id` is a PER-AGENT counter (agents_meta.session_index), so the
    row is keyed by (agent_id, session_id) — deleting on session_id alone
    could drop another agent's same-numbered row (task #1155).
    """
    with write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM agent_watchers WHERE agent_id = %s AND session_id = %s",
            (agent_id, session_id),
        )


def mark_status(agent_id: int, session_id: int, status: str) -> None:
    """Transition a row's status (`running` → terminal history)."""
    if status not in _WATCHER_STATUSES:
        raise ValueError(f"unknown watcher status {status!r}")
    with write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_watchers SET status = %s, updated_at = now() "
            "WHERE agent_id = %s AND session_id = %s",
            (status, agent_id, session_id),
        )


def wake_delivered(agent_id: int, session_id: int, message: str, since: Any) -> bool:
    """Whether a `watcher:<session_id>`-tagged wake with exactly `message`
    already reached the agent after `since` (the row's `created_at`).

    The missed judgment's delivery check (task #1858): a one-shot whose child
    fired and delivered its wake but whose clean-exit row delete failed (or
    raced a boot reconcile) must be dropped silently, not marked `missed` +
    alerted — the wake was NOT lost, so the "your watcher never fired" alert
    is a false alarm (observed 2026-08-27: fired wake at 18:30:00, "marked
    missed" alert at 18:30:02, same session).

    The content match is the discriminator, not an optimization: the shell
    completion notice ("Watcher '<name>' exited with code N...") arrives with
    the same `kind='chat'` + `source='watcher:<sid>'` tag, so a probe on
    kind+source alone would count a child that died BEFORE waking (host
    alive, notice delivered, wake never sent) as delivered and silently drop
    a genuinely missed row (QA review of PR #826, 2026-08-27). Only the wake
    itself carries the row's message verbatim.
    """
    with connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'chat' AND source = %s "
            "AND content = %s AND created_at > %s "
            "LIMIT 1",
            (agent_id, f"watcher:{session_id}", message, since),
        )
        return cur.fetchone() is not None


def watcher_rows(agent_id: int | None = None) -> list[dict[str, Any]]:
    """Every watcher registry row, newest first; `agent_id` narrows to one agent.

    The boot reconcile reads this to decide rebuild vs missed; the stop reaper
    reads the session ids to tell watcher kills apart from plain shell kills.
    """
    with connect(autocommit=True) as conn, conn.cursor(row_factory=dict_row) as cur:
        if agent_id is None:
            cur.execute("SELECT * FROM agent_watchers ORDER BY created_at DESC, session_id DESC")
        else:
            cur.execute(
                "SELECT * FROM agent_watchers WHERE agent_id = %s "
                "ORDER BY created_at DESC, session_id DESC",
                (agent_id,),
            )
        return list(cur.fetchall())


def watcher_session_ids(agent_id: int | None = None) -> set[int]:
    """The session ids the registry knows as watchers (all agents, or one)."""
    with connect(autocommit=True) as conn, conn.cursor() as cur:
        if agent_id is None:
            cur.execute("SELECT session_id FROM agent_watchers")
        else:
            cur.execute("SELECT session_id FROM agent_watchers WHERE agent_id = %s", (agent_id,))
        return {r[0] for r in cur.fetchall()}
