"""Bound delivery-watchdog wake re-dispatches for each pending inbound.

Successful publishes advance a durable per-row counter and timestamp. Once the
configured cap is reached, the watchdog marks the row poisoned, emits one
``delivery_poisoned`` event, and stops re-publishing it. Poisoning is only a
watchdog guard: the inbound remains pending and claimable by a recovered agent.

After fixing the underlying failure, an operator can resume watchdog delivery:
``UPDATE inbound_messages SET dispatch_count = 0, last_dispatch_at = NULL,
poisoned_at = NULL WHERE id = <inbound_id>;``
"""

from __future__ import annotations

import logging
from typing import NamedTuple

from psycopg_pool import ConnectionPool

import shared.db
from shared import telemetry
from shared.db_transaction import write_transaction

_log = logging.getLogger("services.delivery_watchdog.dispatch_guard")


class _PoisonCandidate(NamedTuple):
    inbound_id: int
    agent_id: int
    agent_label: str | None
    dispatch_count: int
    age_s: float


def select_pending_for_dispatch(
    pool: ConnectionPool,
    age_s: float,
    max_dispatch_count: int,
    backoff_steps: list[float],
) -> list[tuple[int, int]]:
    """Return stale pending inbounds eligible for their next wake publish.

    The initial publish is gated by ``age_s``. Later publishes wait for the
    configured step indexed by the row's current dispatch count (1-based),
    repeating the final step when the configured list is shorter than the
    dispatch cap. Poisoned rows, rows at the cap, and owners under an active
    automatic-wake suppression window are never selected.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT m.id, m.agent_id "
            "FROM inbound_messages m "
            "JOIN agents_meta am ON am.id = m.agent_id "
            "WHERE m.status = 'pending' AND am.status = 'idling' "
            "  AND (am.wake_suppressed_until IS NULL OR am.wake_suppressed_until < now()) "
            "  AND m.created_at < now() - make_interval(secs => %s) "
            "  AND m.dispatch_count < %s AND m.poisoned_at IS NULL "
            "  AND (m.last_dispatch_at IS NULL OR m.last_dispatch_at < now() - "
            "       make_interval(secs => (%s::float8[])[LEAST("
            "           GREATEST(m.dispatch_count, 1), array_length(%s::float8[], 1))])) "
            "ORDER BY m.created_at ASC",
            (age_s, max_dispatch_count, backoff_steps, backoff_steps),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def dispatch_wakes(
    pool: ConnectionPool,
    dispatch_threshold_s: float,
    max_dispatch_count: int,
    backoff_steps: list[float],
) -> int:
    """Publish eligible wakes, record successes, then poison exhausted rows.

    Publish failures are logged and do not advance the durable counter. The
    pending-status guard prevents a successful publish racing a claim from
    mutating a claimed row. Poison remains claimable and can be manually reset
    with the SQL documented in this module's docstring. Returns the number of
    successful publishes.
    """
    dispatched_ids: list[int] = []
    for inbound_id, agent_id in select_pending_for_dispatch(
        pool, dispatch_threshold_s, max_dispatch_count, backoff_steps
    ):
        # publish_inbound_wake never raises; it reports delivery through its
        # return value. Only a wake that actually reached Redis advances the
        # counter — a Redis outage must not burn a row's dispatch budget.
        if shared.db.publish_inbound_wake(agent_id, str(inbound_id)):
            dispatched_ids.append(inbound_id)

    if dispatched_ids:
        with write_transaction(pool) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages "
                "SET dispatch_count = dispatch_count + 1, "
                "    last_dispatch_at = clock_timestamp() "
                "WHERE id = ANY(%s) AND status = 'pending'",
                (dispatched_ids,),
            )

    _poison_exhausted_dispatches(pool, max_dispatch_count)
    return len(dispatched_ids)


def _poison_exhausted_dispatches(pool: ConnectionPool, max_dispatch_count: int) -> int:
    candidates = _select_poison_candidates(pool, max_dispatch_count)
    if not candidates:
        return 0

    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages SET poisoned_at = clock_timestamp() "
            "WHERE id = ANY(%s) AND poisoned_at IS NULL AND status = 'pending' "
            "RETURNING id",
            ([candidate.inbound_id for candidate in candidates],),
        )
        poisoned_ids = {row[0] for row in cur.fetchall()}

    for candidate in candidates:
        if candidate.inbound_id in poisoned_ids:
            _alert_poisoned(candidate)
    return len(poisoned_ids)


def _select_poison_candidates(
    pool: ConnectionPool, max_dispatch_count: int
) -> list[_PoisonCandidate]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT m.id, m.agent_id, a.label, m.dispatch_count, "
            "       EXTRACT(EPOCH FROM (now() - m.created_at)) AS age_s "
            "FROM inbound_messages m "
            "LEFT JOIN agents a ON a.id = m.agent_id "
            "WHERE m.status = 'pending' AND m.dispatch_count >= %s "
            "  AND m.poisoned_at IS NULL "
            "ORDER BY m.created_at ASC",
            (max_dispatch_count,),
        )
        return [
            _PoisonCandidate(row[0], row[1], row[2], row[3], float(row[4]))
            for row in cur.fetchall()
        ]


def _alert_poisoned(candidate: _PoisonCandidate) -> None:
    _log.warning(
        "[delivery] inbound %s to agent %s (%s) poisoned after %s wake re-dispatches "
        "and %.0fs pending; watchdog re-dispatch stopped",
        candidate.inbound_id,
        candidate.agent_id,
        candidate.agent_label or f"#{candidate.agent_id}",
        candidate.dispatch_count,
        candidate.age_s,
    )
    try:
        telemetry.emit(
            "telemetry",
            "delivery_poisoned",
            level="warning",
            agent_id=candidate.agent_id,
            source="system",
            attributes={
                "inbound_id": candidate.inbound_id,
                "dispatch_count": candidate.dispatch_count,
                "age_s": candidate.age_s,
            },
        )
    except Exception:
        _log.exception(
            "[delivery] delivery_poisoned emit failed for inbound %s", candidate.inbound_id
        )
