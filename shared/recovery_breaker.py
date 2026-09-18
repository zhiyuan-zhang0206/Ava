"""Durable recovery circuit breaker for permanent provider rejections.

Invariant (task #3617, design #3610 section 12): two CONSECUTIVE permanent-class
provider rejections with no successful turn between them halt every automatic
recovery path for that agent — event-path resurrect, the delivery watchdog's
wake re-dispatch and terminated-owner retry, the stalled crash-marked harvest
op, and the relaxed reaper-marked trigger — until a turn succeeds. A manual
resurrect (`resurrect-explicit-v2`, no pending-work trigger) stays exempt: it is
the explicit human override.

`agents_meta.permanent_reject_streak` is the durable count and the single
source of truth for "halted": incremented when a turn ends with a
permanent-class ``FatalProviderError`` (``agent/_runloop.py``), reset to 0 by
the completed-turn UPDATE that clears ``last_turn_fatal_at``
(``agent/graph/_llm.py::_persist_last_active``). The latest rejection's
reason class lives beside the count in ``agents_meta.last_permanent_reject_reason``
(written with the increment, cleared with the streak). Gates read the streak
(``RECOVERY_BREAKER_CLEAR``), never the wake-suppression window alone: a claim
(e.g. a heartbeat note) clears ``wake_suppressed_until`` by design, so the
suppression column cannot carry an until-human halt.

Tripping additionally writes ``wake_suppressed_until`` with
``SUPPRESS_REASON_PERMANENT_REJECT`` — the operator-visible reason, and the
input to the suppression gates the automatic paths already consult — while the
caller (``agent/_runloop.py``) reuses the metadata-only ancestor report plus a
blocked Error event for the honest UX.
"""

from __future__ import annotations

from typing import Final, LiteralString, cast

from psycopg_pool import AsyncConnectionPool

from shared.db_transaction import async_write_transaction

HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS = 2

SUPPRESS_REASON_PERMANENT_REJECT = "permanent_provider_reject"

# The tripped window is "until human" in practice: long enough that no
# automatic path resumes on a mere timer, finite so the column stays
# representable everywhere (no `infinity` datetime edge cases).
HALT_SUPPRESSION_WINDOW_S = 3650 * 24 * 3600.0

# The durable reject reason for an HTTP 402 billing rejection — the value
# `agent._runloop._circuit_reason` maps 402 to (CIRCUIT_REASON_BILLING). The
# billing batch-recovery whitelist (task #3919) filters on exactly this
# value; keep it in sync with CIRCUIT_REASON_BILLING (asserted by
# tests/shared/test_recovery_breaker.py, same pattern as RECOVERY_BREAKER_CLEAR).
PERMANENT_REJECT_REASON_BILLING: Final = "billing"

# Correlated against the unaliased agents_meta row (same shape as
# `shared.lifecycle_acceptance.FAILED_RESTART_FOR_CURRENT_TARGET`). This is the
# SQL half of the breaker the automatic-recovery gates embed; keep the literal
# in sync with HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS (asserted by
# tests/shared/test_recovery_breaker.py).
RECOVERY_BREAKER_CLEAR: LiteralString = "permanent_reject_streak < 2"


async def record_permanent_reject_turn(
    pool: AsyncConnectionPool, agent_id: int, reason: str
) -> int:
    """Count one permanent-class rejected turn; return the new streak.

    `reason` is the ``_circuit_reason`` class of THIS rejection; it is
    recorded beside the streak (``last_permanent_reject_reason``) so durable
    readers — the billing batch-recovery whitelist (task #3919) — can tell
    why the streak stands, not just how tall it is. Callers keep this
    best-effort (log-and-continue on failure): the breaker must never mask
    the original provider rejection.
    """
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE agents_meta SET permanent_reject_streak = permanent_reject_streak + 1, "
            "last_permanent_reject_reason = %s "
            "WHERE id = %s RETURNING permanent_reject_streak",
            (reason, agent_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"agent {agent_id} disappeared while recording a permanent rejection")
    return cast(int, row[0])


async def halt_automatic_recovery(pool: AsyncConnectionPool, agent_id: int) -> bool:
    """Trip the breaker: suppress automatic wakes for `agent_id`.

    Idempotent against an already-tripped row: while the permanent-reject
    suppression is active it is left untouched (the streak is the durable
    gate; this write is the operator-visible reason and the input to the
    suppression gates). Any other reason's window — or an expired one — is
    replaced, because this trip is the stronger, until-human state.

    Returns True when this call wrote a fresh suppression window.
    """
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE agents_meta "
            "SET wake_suppressed_until = clock_timestamp() + make_interval(secs => %s), "
            "    wake_suppress_reason = %s "
            "WHERE id = %s "
            "  AND (wake_suppress_reason IS DISTINCT FROM %s "
            "       OR wake_suppressed_until IS NULL "
            "       OR wake_suppressed_until < now())",
            (
                HALT_SUPPRESSION_WINDOW_S,
                SUPPRESS_REASON_PERMANENT_REJECT,
                agent_id,
                SUPPRESS_REASON_PERMANENT_REJECT,
            ),
        )
        return cur.rowcount == 1
