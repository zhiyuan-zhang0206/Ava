"""Stale-inbound dead-letter sweeps — delivery watchdog job 4.

Split out of `daemon.py` when the closure guard pushed that module at its line
budget. Each sweep closes only rows whose consumer is gone: a stale claimed
chat of a terminated/idling owner (boot reconcile cannot finalize it), a
pending resurrect whose wake was abandoned, a one-shot lifecycle notice of a
terminated owner, or a pending chat past the G4 resurrect-retry age gate
(issue #2049 — the retry selector and this sweep share that gate, in lockstep).
"""

from psycopg_pool import ConnectionPool

from shared.db_transaction import write_transaction


def dead_letter_stale_pending_chats(pool: ConnectionPool, threshold_s: float) -> int:
    """Dead-letter stale pending chats whose terminated owner never claimed them.

    A pending chat newer than the latest termination makes its terminated
    owner a G4 resurrect candidate on every tick. That retry must not run
    forever: a chat that stays pending past `threshold_s` is a dead letter, so
    the reaper closes it here (issue #2049) — marked done, never deleted — and
    the resurrect-suicide loop can no longer accumulate an ever-growing
    pending queue. `select_terminated_owners_with_pending` applies the same
    age gate, so the two stay in lockstep: a dead-lettered row is never a
    trigger and a trigger row is never dead-lettered. Live owners are untouched; their pending
    chats still wake through the normal dispatch path.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages m SET status = 'done', claimed_at = now() "
            "FROM agents_meta am "
            "WHERE m.agent_id = am.id "
            "  AND am.status = 'terminated' "
            "  AND m.status = 'pending' AND m.kind = 'chat' "
            "  AND m.created_at < now() - make_interval(secs => %s)",
            (threshold_s,),
        )
        return cur.rowcount


def dead_letter_stale_claimed(
    pool: ConnectionPool,
    threshold_s: float,
    idling_threshold_s: float = 7200.0,
) -> int:
    """Dead-letter stale claimed chats of terminated or idling owners.

    Terminated agents leave 'claimed' rows behind: reconcile runs only at
    process boot, so a cleanly terminated (or long-dead) process never
    finalizes its claims. The rows sit forever — and worse, if the agent is
    ever resurrected (delivery auto-resurrect, Task #689 G4, or manually),
    boot reconcile sees no commit evidence (checkpoint pruned) and resets
    them all to 'pending', re-delivering ancient messages as fresh ones
    (Task #654). Dead-lettering rows older than the threshold keeps the
    two-phase crash-recovery guarantee (rows younger than the threshold still
    reset to 'pending' on boot) while making a resurrected agent start from
    its real conversation, not a flood of stale mail.

    Age is measured from `claimed_at`, falling back to `created_at` for rows
    that predate the claimed_at column (2026-08-02): a NULL claimed_at means
    'claimed before the column existed', so created_at is the only age
    evidence left. Running owners are never touched; idling owners are swept
    past the idling threshold because hosted agents may never boot again to
    finalize their claims.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages m SET status = 'done' "
            "FROM agents_meta am "
            "WHERE m.agent_id = am.id "
            "  AND m.status = 'claimed' AND m.kind = 'chat' "
            "  AND ((am.status = 'terminated' "
            "        AND COALESCE(m.claimed_at, m.created_at) "
            "            < now() - make_interval(secs => %s)) "
            "       OR (am.status = 'idling' "
            "           AND COALESCE(m.claimed_at, m.created_at) "
            "               < now() - make_interval(secs => %s)))",
            (threshold_s, idling_threshold_s),
        )
        return cur.rowcount


def dead_letter_stale_pending_resurrects(pool: ConnectionPool, threshold_s: float) -> int:
    """Dead-letter pending resurrect rows whose consumer never reached claim.

    A stale lifecycle row records an abandoned wake. Retaining it cannot
    recover the turn and later floods the agent with redundant markers, so age
    alone decides cleanup regardless of the current agent lifecycle state.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages m SET status = 'done', claimed_at = now() "
            "WHERE m.status = 'pending' AND m.kind = 'resurrect' "
            "  AND m.created_at < now() - make_interval(secs => %s)",
            (threshold_s,),
        )
        return cur.rowcount


def dead_letter_stale_pending_terminated(pool: ConnectionPool, threshold_s: float) -> int:
    """Complete stale lifecycle notices whose terminated owner cannot claim them.

    Post-termination chats remain pending for the G4 resurrect-retry path; only
    one-shot lifecycle notices with no remaining consumer are dead-lettered.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages m SET status = 'done', claimed_at = now() "
            "FROM agents_meta am "
            "WHERE m.agent_id = am.id "
            "  AND am.status = 'terminated' "
            "  AND m.status = 'pending' "
            "  AND m.kind IN ('terminate', 'system_note', 'restart_completed') "
            "  AND m.created_at < now() - make_interval(secs => %s)",
            (threshold_s,),
        )
        return cur.rowcount
