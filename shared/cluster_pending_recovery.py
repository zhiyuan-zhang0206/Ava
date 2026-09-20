"""Takeover and abandon CASes for the pending-publication protocols.

A durable `pending` managed-writer publication (`managed_writer_evidence`) may
never be stranded by generic lease handling — `claim_recovery_lock` refuses it
by design — so its protocols have their own transitions on the same
`deployment_state` row: `claim_pending_recovery_lease` replaces ONLY an
executing rollout whose journaled pending operation still matches the exact
subdocument the caller inspected and whose proven-dead lease identity still
matches that proof, and `abandon_pending_publication_lease` is the exact
pre-stop abort's single guarded write — clearing a never-effective journal and
releasing its abandoned lease together, under the same exactness. The general
lease mutations live in `shared.cluster_lock`; this module is split out to keep
that module inside the file-size budget and carries the transitions generic
recovery must not own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from shared.cluster_lock import LOCK_TTL_S
from shared.db_transaction import write_transaction
from shared.log import logger


@dataclass(frozen=True)
class PendingLeaseClaim:
    """Result of taking over an abandoned pending publication's rollout lease.

    ``acquired`` False means the inspected journal or lease changed before the
    CAS — another recovery won, or the pending operation was replaced — and
    nothing was replaced. On success the fields carry the new live lease's exact
    server-side identity; the caller builds the replacement rollout identity
    from them (this module deliberately does not import the publication types).
    """

    acquired: bool
    previous_holder: str | None = None
    acquired_at: datetime | None = None
    target_sha: str | None = None


def claim_pending_recovery_lease(
    holder: str,
    *,
    expected_operation: dict[str, Any],
    observed: tuple[str, datetime] | None,
    ttl_s: float = LOCK_TTL_S,
) -> PendingLeaseClaim:
    """CAS-takeover of the deploy row for the pending-publication recovery protocol.

    `claim_recovery_lock` may never strand durable publication evidence; this is
    that evidence's own takeover, so the row must be an executing rollout
    (``phase='updating'``, ``kind='rollout'``, ``settle_hosts IS NULL``) whose
    ``managed_writer_evidence.pending.operation`` still equals
    ``expected_operation`` — the exact JSONB subdocument the caller inspected.
    The caller has already proven the previous holder's process gone; the
    ``observed`` pin (holder + ``acquired_at``) makes that proof specific — a
    lease is replaceable only while both still match it — so a new rollout that
    lands after the proof refuses instead of being clobbered. With
    ``observed=None`` only a free or expired row is claimable. On success the row
    becomes ``holder``'s fresh live lease; ``target_sha`` is deliberately
    preserved (the replacement operation resumes the same rollout target).
    """
    observed_holder = observed[0] if observed is not None else None
    observed_acquired_at = observed[1] if observed is not None else None
    if observed is not None and observed_acquired_at is None:
        logger.warning(
            "[cluster-lock] pending recovery claim refused: observed lease lacks "
            "acquired_at identity"
        )
        return PendingLeaseClaim(acquired=False)
    with write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "WITH prev AS MATERIALIZED ("
            "  SELECT holder, target_sha FROM deployment_state WHERE id = 1 "
            "  AND phase = 'updating' AND kind = 'rollout' AND settle_hosts IS NULL "
            "  AND target_sha IS NOT NULL "
            "  AND managed_writer_evidence->'pending'->'operation' = %s "
            "  AND ("
            "    (%s::text IS NULL AND (holder IS NULL OR expires_at < now())) OR "
            "    (%s::text IS NOT NULL AND holder = %s AND acquired_at = %s)"
            "  ) FOR UPDATE"
            "), claimed AS ("
            "  UPDATE deployment_state SET holder = %s, acquired_at = now(), "
            "    expires_at = now() + make_interval(secs => %s), "
            "    settle_hosts = NULL, settle_note = NULL, settle_started_at = NULL, "
            "    phase = 'updating', kind = 'rollout' "
            "  WHERE id = 1 AND EXISTS (SELECT 1 FROM prev) "
            "  RETURNING acquired_at, target_sha"
            ") SELECT (SELECT count(*) FROM claimed), (SELECT holder FROM prev), "
            "       (SELECT acquired_at FROM claimed), (SELECT target_sha FROM claimed)",
            (
                Jsonb(expected_operation),
                observed_holder,
                observed_holder,
                observed_holder,
                observed_acquired_at,
                holder,
                ttl_s,
            ),
        )
        row = cur.fetchone()
    acquired = row is not None and row[0] == 1
    if not acquired or row is None:
        logger.warning(
            "[cluster-lock] pending recovery claim by {holder} refused: journal or lease changed",
            holder=holder,
        )
        return PendingLeaseClaim(acquired=False)
    claim = PendingLeaseClaim(
        acquired=True, previous_holder=row[1], acquired_at=row[2], target_sha=row[3]
    )
    logger.warning(
        "[cluster-lock] pending recovery claimed by {holder}; replaced {previous}",
        holder=holder,
        previous=claim.previous_holder,
    )
    return claim


def abandon_pending_publication_lease(
    conn: psycopg.Connection,
    *,
    expected_operation: dict[str, Any],
    observed: tuple[str, datetime] | None,
) -> bool:
    """Clear a never-effective pending journal and release its abandoned lease, atomically.

    The exact pre-stop abort's one write (task #4129 C-4): only an executing
    rollout row whose journaled ``pending.operation`` still equals
    ``expected_operation`` -- the subdocument the caller proved unit-effect-free
    -- AND whose lease identity still matches the proven-dead ``observed``
    pin (or is free/expired) is cleared; the write also refuses when the
    journal already records ``collection`` / ``migration`` / ``unit_readbacks``,
    because those are effects' records, never this path's to drop.
    ``managed_writer_evidence.current`` is preserved. Runs in the caller's
    transaction (the journal re-lock and this write share one lock window);
    this module never opens one of its own.

    Returns True when the row was cleared; False means the journal or the lease
    changed since the caller's inspection and nothing was touched.
    """
    observed_holder = observed[0] if observed is not None else None
    observed_acquired_at = observed[1] if observed is not None else None
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE deployment_state SET holder = NULL, acquired_at = NULL, expires_at = NULL, "
            "    settle_hosts = NULL, settle_note = NULL, settle_started_at = NULL, "
            "    phase = 'stable', kind = NULL, "
            "    managed_writer_evidence = jsonb_set("
            "        managed_writer_evidence, '{pending}', 'null'::jsonb) "
            "WHERE id = 1 AND phase = 'updating' AND kind = 'rollout' AND settle_hosts IS NULL "
            "AND target_sha IS NOT NULL "
            "AND managed_writer_evidence->'pending'->'operation' = %s "
            "AND managed_writer_evidence->'pending'->'collection' = 'null'::jsonb "
            "AND managed_writer_evidence->'pending'->'migration' = 'null'::jsonb "
            "AND managed_writer_evidence->'pending'->'unit_readbacks' = '[]'::jsonb "
            "AND ("
            "    (%s::text IS NULL AND (holder IS NULL OR expires_at < now())) OR "
            "    (%s::text IS NOT NULL AND holder = %s AND acquired_at = %s)"
            ")",
            (
                Jsonb(expected_operation),
                observed_holder,
                observed_holder,
                observed_holder,
                observed_acquired_at,
            ),
        )
        cleared = cur.rowcount == 1
    if not cleared:
        logger.warning("[cluster-lock] pre-stop abandon refused: journal or lease changed")
    return cleared
