"""Durable attempt clocks for the watchdog's recovery loops.

Each recovery loop (resurrect retry, stalled crash-marked harvest, hosted-turn
wedge recovery) acts on an agent at most once per cooldown. The clock lives in
`delivery_watchdog_attempts`, one row per `(kind, agent_id)`, so a watchdog
restart resumes the cooldown instead of re-attempting every candidate at once.

All functions are synchronous psycopg; the loops call them through
`asyncio.to_thread`.
"""

from __future__ import annotations

from collections.abc import Sequence

from psycopg_pool import ConnectionPool

from base.db.transaction import write_transaction

RESURRECT = "resurrect"
HARVEST = "harvest"
HOSTED_TURN = "hosted_turn"


def claim_attempts(
    pool: ConnectionPool,
    kind: str,
    agent_ids: Sequence[int],
    cooldown_s: float,
    limit: int | None = None,
) -> tuple[list[int], int]:
    """Claim, in order, the agents whose `kind` cooldown has elapsed.

    A claim stamps `last_attempt_at` in the same statement that checks the
    cooldown, so an agent is handed out once per cooldown even across
    restarts. At most `limit` agents are claimed; the second element counts the
    ready agents left unclaimed by it, which the next round picks up. An agent
    that no longer exists is never claimed.
    """
    claimed: list[int] = []
    deferred = 0
    with write_transaction(pool) as conn, conn.cursor() as cur:
        for agent_id in agent_ids:
            if limit is not None and len(claimed) >= limit:
                cur.execute(
                    "SELECT 1 FROM delivery_watchdog_attempts "
                    "WHERE kind = %s AND agent_id = %s "
                    "AND last_attempt_at >= clock_timestamp() - make_interval(secs => %s)",
                    (kind, agent_id, cooldown_s),
                )
                if cur.fetchone() is None:
                    deferred += 1
                continue
            cur.execute(
                "INSERT INTO delivery_watchdog_attempts (kind, agent_id) "
                "SELECT %s, id FROM agents WHERE id = %s "
                "ON CONFLICT (kind, agent_id) DO UPDATE SET last_attempt_at = clock_timestamp() "
                "WHERE delivery_watchdog_attempts.last_attempt_at "
                "      < clock_timestamp() - make_interval(secs => %s) "
                "RETURNING agent_id",
                (kind, agent_id, cooldown_s),
            )
            if cur.fetchone() is not None:
                claimed.append(agent_id)
    return claimed, deferred


def finish_attempt(pool: ConnectionPool, kind: str, agent_id: int) -> None:
    """Restart the cooldown from the attempt's end: a slow failing RPC must
    not leave the next attempt immediately due."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE delivery_watchdog_attempts SET last_attempt_at = clock_timestamp() "
            "WHERE kind = %s AND agent_id = %s",
            (kind, agent_id),
        )
