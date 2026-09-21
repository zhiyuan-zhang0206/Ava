"""Lifecycle-pointer maintenance scans for the gateway TTL reaper.

Two slow, hourly scans over ``agents_meta.lifecycle_command_id`` — the one
durable pointer that says which accepted lifecycle command (restart /
terminate) an agent still owes:

- ``_scan_torn_lifecycle_pointers_blocking`` (task #3678): a detect-only
  safety net for commands sitting at ``done`` while the pointer is still
  alive — the shape that blinds boot recovery and live observation at once.
- ``settle_absent_machine_fences`` (task #4143): settles applied-but-
  unobserved force-terminate commands whose agent's home machine is absent
  from the machines registry. A decommissioned machine never runs the boot
  recovery that normally observes its fences, so they would sit stuck
  forever.

Split out of ``gateway/ttl_reaper.py`` (which sits at the 800-line hard
ceiling) so both pointer scans live in one focused module beside the reaper
loop that schedules them.
"""

from __future__ import annotations

import logging
import time
from uuid import UUID

from psycopg_pool import ConnectionPool

from shared import telemetry
from shared.db_transaction import write_transaction

_log = logging.getLogger(__name__)

# Torn lifecycle-pointer scan cadence: the commit-time guard (task #3678)
# rejects new pointer->done writes; this scan is the slow safety net for the
# sides the guard cannot see (a bypass write, a rollback past the migration).
_TORN_POINTER_SCAN_INTERVAL_S = 3600.0
_torn_pointer_last_scan: float | None = None

# Absent-machine fence-settle cadence: the rows stay stuck until settled and
# nothing else ever settles them, so hourly is prompt enough — and the scan is
# hourly, not per-poll, for the same reason as the torn-pointer one (a
# pointer join every pass would scan agents_meta for nothing).
_FENCE_SETTLE_INTERVAL_S = 3600.0
_fence_settle_last_scan: float | None = None


def _torn_pointer_scan_due() -> bool:
    """True once per torn-pointer scan interval (hourly).

    Same throttle shape as the fire-log prune: the scan is hourly, not a
    per-poll job (a live-pointer join every 30 s would scan agents_meta for
    nothing).
    """
    global _torn_pointer_last_scan  # noqa: PLW0603 — process-local scan cadence
    now = time.monotonic()
    if (
        _torn_pointer_last_scan is None
        or now - _torn_pointer_last_scan >= _TORN_POINTER_SCAN_INTERVAL_S
    ):
        _torn_pointer_last_scan = now
        return True
    return False


def _fence_settle_due() -> bool:
    """True once per absent-machine fence-settle scan interval (hourly)."""
    global _fence_settle_last_scan  # noqa: PLW0603 — process-local scan cadence
    now = time.monotonic()
    if _fence_settle_last_scan is None or now - _fence_settle_last_scan >= _FENCE_SETTLE_INTERVAL_S:
        _fence_settle_last_scan = now
        return True
    return False


def _scan_torn_lifecycle_pointers_blocking(pool: ConnectionPool) -> int:
    """Count lifecycle commands sitting at `done` while agents_meta still
    points at them; alert when any is found.

    That torn shape blinds boot recovery (it needs the command still
    `claimed`) and live observation (it needs a live process identity) at
    once, so any resurrect of the affected agent defers forever — 6285/200306
    and 6089/172352 (task #3678). The commit-time guard rejects the inbound
    side of the shape; this scan is the slower safety net for the sides it
    cannot see.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM agents_meta m "
            "JOIN inbound_messages i ON i.id = m.lifecycle_command_id AND i.agent_id = m.id "
            "WHERE i.status = 'done'"
        )
        row = cur.fetchone()
        count = 0 if row is None else int(row[0])
        if not count:
            return 0
        cur.execute(
            "SELECT m.id FROM agents_meta m "
            "JOIN inbound_messages i ON i.id = m.lifecycle_command_id AND i.agent_id = m.id "
            "WHERE i.status = 'done' ORDER BY m.id LIMIT 5"
        )
        samples = [int(r[0]) for r in cur.fetchall()]
    _log.warning(
        "[ttl-reaper] %d lifecycle command(s) sit at done with a live pointer "
        "(agent(s) %s) — resurrection of the named agent(s) defers until settled",
        count,
        ", ".join(str(a) for a in samples),
    )
    telemetry.emit(
        "log",
        "lifecycle_pointer_done_torn",
        level="warning",
        attributes={"count": count, "samples": ", ".join(str(a) for a in samples)},
    )
    return count


def _settle_one_fence_blocking(
    pool: ConnectionPool,
    agent_id: int,
    command_id: int,
    generation: UUID,
    owner: UUID,
) -> bool:
    """Settle one fence under the agent row lock; True when this call settled it.

    Re-verifies the exact command target and the machine's continued absence
    inside the locking transaction: a machine that re-registers in between is
    a live cluster member again, and its own boot recovery owns its fences
    (``shared.hosted_force.recover_orphaned_hosted_forces``).
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT lifecycle_command_id, runtime_generation, runtime_owner, machine "
            "FROM agents_meta WHERE id = %s FOR UPDATE",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None or row[0] != command_id or row[1] != generation or row[2] != owner:
            return False
        cur.execute("SELECT 1 FROM machines WHERE name = %s", (row[3],))
        if cur.fetchone() is not None:
            return False
        cur.execute(
            "UPDATE inbound_messages SET observed_at = clock_timestamp(), status = 'done' "
            "WHERE id = %s AND agent_id = %s AND kind = 'terminate' AND status = 'claimed' "
            "AND applied_at IS NOT NULL AND observed_at IS NULL "
            "AND target_generation = %s AND target_owner = %s",
            (command_id, agent_id, generation, owner),
        )
        if cur.rowcount != 1:
            return False
        cur.execute(
            "UPDATE agents_meta SET lifecycle_command_id = NULL, lease_expires_at = NULL "
            "WHERE id = %s AND lifecycle_command_id = %s",
            (agent_id, command_id),
        )
        if cur.rowcount != 1:  # pragma: no cover — the row lock above owns the pointer
            raise RuntimeError("absent-machine fence settle lost its locked pointer")
        return True


def settle_absent_machine_fences(pool: ConnectionPool, *, batch: int) -> list[int]:
    """Settle force-terminate fences of terminated agents whose machine is gone.

    A hosted force-terminate command is installed claimed+applied in one write
    (`shared.hosted_force.install_hosted_force`) and settled — observed, its
    ``agents_meta.lifecycle_command_id`` pointer cleared — by the machine's own
    recovery: the original host's serialized turn pump (`original_host_force`)
    or its next boot (`recover_orphaned_hosted_forces`). A decommissioned
    machine never boots again and has no pump left, and nothing else observes a
    claimed command, so every one of its agents' fences sits stuck forever (the
    machine-pause sweep leaves one per agent; task #4143) — and the live
    pointer keeps that agent's lifecycle blocked for any later resurrect.

    This scan is the machine-independent settler those rows were missing:
    agents terminated with a claimed, applied, unobserved terminate command
    whose machine has no ``machines`` row are settled to the exact transition
    the boot recovery would have written. Pending (never-claimed) terminate
    fences are deliberately NOT in scope — the delivery watchdog's existing
    stale sweep closes those by age.

    Bounded by `batch` per pass (the reaper's per-pass ceiling), so a backlog
    drains over successive passes. Returns the settled agent ids.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, f.id, f.target_generation, f.target_owner
            FROM agents_meta m
            JOIN inbound_messages f ON f.id = m.lifecycle_command_id AND f.agent_id = m.id
            WHERE m.status = 'terminated'
              AND f.kind = 'terminate'
              AND f.status = 'claimed'
              AND f.applied_at IS NOT NULL
              AND f.observed_at IS NULL
              AND f.target_generation = m.runtime_generation
              AND f.target_owner = m.runtime_owner
              AND NOT EXISTS (SELECT 1 FROM machines mm WHERE mm.name = m.machine)
            ORDER BY m.id
            LIMIT %s
            """,
            (batch,),
        )
        candidates = cur.fetchall()
    settled: list[int] = []
    for agent_id, command_id, generation, owner in candidates:
        if _settle_one_fence_blocking(pool, int(agent_id), int(command_id), generation, owner):
            settled.append(int(agent_id))
    if settled:
        samples = ", ".join(str(a) for a in settled[:5])
        _log.info(
            "[ttl-reaper] settled %d lifecycle fence(s) of absent machine(s): agent(s) %s",
            len(settled),
            samples,
        )
        telemetry.emit(
            "log",
            "lifecycle_fences_settled_absent_machine",
            level="info",
            attributes={"count": len(settled), "samples": samples},
        )
    return settled
