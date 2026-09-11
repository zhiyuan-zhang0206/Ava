"""Read-only per-target cohort readiness report for `ava cluster update`.

Why this exists — the bootstrap hazard: a runner's Phase A drain runs the
runner's OWN deployed code, and on pre-fix code an idle hosted agent's held
wake is deferred silently, so the drain burns its whole hold and the rollout
aborts with `hold retained for agent(s) [...]` after the fact, with no
a-priori signal. This report classifies every agent-runner target's
non-terminated cohort BEFORE the operator starts, so the same shape is visible
in seconds:

- empty cohort                        -> the drain trivially passes;
- ``idle-hosted`` rows                -> consumed via the held-wake path,
                                          which stalls on pre-fix runner code;
- ``restarting`` / expired-lease rows -> drain-blocking residue to clear first.

The report is READ-ONLY (SELECTs only) and best-effort by contract: when the
database is unreachable, the dry run it annotates prints ``(skipped: ...)``
instead of failing.

It is a coarse PRE-FLIGHT PROXY of the drain's own classification
(``shared/maintenance_cohort.py``): it reads ``agents_meta`` fields quickly and
errs toward showing rows as ordinary — an owner present with a live lease that
is not the *active* native owner still renders as ``running`` / ``idle-hosted``,
while the real drain refuses it ("maintenance requires the live original native
owner"). The drain's classification remains the authority: a clean report is no
guarantee, a blocked report is a strong signal. ``lifecycle-pending`` here means
a non-null ``agents_meta.lifecycle_command_id`` — not the ``maintenance_cold``
"unsettled exec request" signal.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from typing import IO

# Bucket names; BUCKET_ORDER is the (worst-first) order every summary prints in.
RESTARTING = "restarting"
STALE_LEASE = "stale-lease"
EXEC_PENDING = "lifecycle-pending"
IDLE_HOSTED = "idle-hosted"
RUNNING = "running"
IDLE_UNHOSTED = "idling-unhosted"
OTHER = "other"

BUCKET_ORDER = (
    RESTARTING,
    STALE_LEASE,
    EXEC_PENDING,
    IDLE_HOSTED,
    RUNNING,
    IDLE_UNHOSTED,
    OTHER,
)

# Buckets whose rows can make a drain refuse before it even starts.
BLOCKING_BUCKETS = frozenset({RESTARTING, STALE_LEASE})

BUCKET_NOTES = {
    RESTARTING: "mid-restart; the drain may refuse until it settles",
    STALE_LEASE: "runtime owner's lease expired; the drain refuses a dead owner",
    EXEC_PENDING: "unsettled lifecycle command on the row",
    IDLE_HOSTED: "needs the held-wake path; a pre-fix runner stalls here",
    RUNNING: "active; the drain requests a maintenance restart",
    IDLE_UNHOSTED: "idle without a runtime owner (cold path)",
    OTHER: "active row",
}


@dataclass(frozen=True)
class CohortRow:
    """One non-terminated `agents_meta` row, reduced to the fields classified on."""

    agent_id: int
    status: str
    has_owner: bool  # runtime_owner IS NOT NULL
    lease_expired: bool  # runtime_owner IS NOT NULL AND lease_expires_at <= now()
    lifecycle_pending: bool  # lifecycle_command_id IS NOT NULL


@dataclass(frozen=True)
class TargetCohort:
    """One agent-runner-capable machine and the cohort a rollout would drain there.

    `inclusion` is "included" for a rollout target, else why the fan-out skips
    it ("staging" / "stopped" / "paused") — the same three latches
    `shared.machines.list_agent_runners()` excludes.
    """

    machine: str
    inclusion: str
    rows: tuple[CohortRow, ...]


def classify_row(row: CohortRow) -> str:
    """The bucket for one row — a pure function of the fields the SQL selected."""
    if row.status == "restarting":
        return RESTARTING
    if row.has_owner and row.lease_expired:
        return STALE_LEASE
    if row.status == "idling" and row.has_owner:
        return IDLE_HOSTED
    if row.lifecycle_pending:
        return EXEC_PENDING
    if row.status == "idling":
        return IDLE_UNHOSTED
    return RUNNING if row.has_owner else OTHER


def collect_targets() -> list[TargetCohort]:
    """Read every agent-runner-capable machine with its classified cohort (SELECTs only)."""
    from shared.db import connect

    with connect() as conn:
        machines = conn.execute(
            "SELECT name, is_staging, (stopped_at IS NOT NULL), (paused_at IS NOT NULL) "
            "FROM machines WHERE 'agent-runner' = ANY(role) ORDER BY name"
        ).fetchall()
        names = [str(m[0]) for m in machines]
        rows = conn.execute(
            "SELECT machine, id, status, (runtime_owner IS NOT NULL), "
            "(runtime_owner IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at <= now()), "
            "(lifecycle_command_id IS NOT NULL) "
            "FROM agents_meta WHERE status <> 'terminated' AND machine = ANY(%s) "
            "ORDER BY machine, id",
            (names,),
        ).fetchall()

    by_machine: dict[str, list[CohortRow]] = {name: [] for name in names}
    for machine, agent_id, status, has_owner, lease_expired, lifecycle_pending in rows:
        by_machine[str(machine)].append(
            CohortRow(
                agent_id=int(agent_id),
                status=str(status),
                has_owner=bool(has_owner),
                lease_expired=bool(lease_expired),
                lifecycle_pending=bool(lifecycle_pending),
            )
        )
    targets: list[TargetCohort] = []
    for name, is_staging, stopped, paused in machines:
        if is_staging:
            inclusion = "staging"
        elif stopped:
            inclusion = "stopped"
        elif paused:
            inclusion = "paused"
        else:
            inclusion = "included"
        targets.append(
            TargetCohort(machine=str(name), inclusion=inclusion, rows=tuple(by_machine[str(name)]))
        )
    return targets


def report_lines(targets: list[TargetCohort]) -> list[str]:
    """Render the readiness report; I/O-free so tests can pin the format."""
    lines = ["→ per-target cohort readiness (read-only):"]
    skipped: list[str] = []
    blocking_total = 0
    idle_hosted_total = 0
    for target in targets:
        if target.inclusion != "included":
            skipped.append(f"{target.machine} ({target.inclusion})")
            continue
        if not target.rows:
            lines.append(f"  ✓ {target.machine}: cohort empty — the drain trivially passes")
            continue
        counts = Counter(classify_row(row) for row in target.rows)
        blocking = sum(counts[bucket] for bucket in BLOCKING_BUCKETS)
        blocking_total += blocking
        idle_hosted_total += counts[IDLE_HOSTED]
        mark = "✗" if blocking else "⚠" if (counts[IDLE_HOSTED] or counts[EXEC_PENDING]) else "✓"
        summary = ", ".join(
            f"{counts[bucket]}x {bucket}" for bucket in BUCKET_ORDER if counts[bucket]
        )
        suffix = f" [{blocking} blocking]" if blocking else ""
        lines.append(
            f"  {mark} {target.machine}: {len(target.rows)} non-terminated — {summary}{suffix}"
        )
        shown: set[str] = set()
        for bucket in BUCKET_ORDER:
            if not counts[bucket]:
                continue
            if bucket in BLOCKING_BUCKETS:
                for row in target.rows:
                    if classify_row(row) == bucket:
                        lines.append(f"      ✗ #{row.agent_id} {bucket}: {BUCKET_NOTES[bucket]}")
            elif bucket in (IDLE_HOSTED, EXEC_PENDING) and bucket not in shown:
                lines.append(f"      · {counts[bucket]}x {bucket}: {BUCKET_NOTES[bucket]}")
                shown.add(bucket)
    if skipped:
        lines.append(f"  (skipped: {', '.join(skipped)})")
    if blocking_total:
        lines.append(f"  → verdict: NOT ready — clear {blocking_total} blocking row(s) first")
    elif idle_hosted_total:
        lines.append(
            f"  → verdict: attempt allowed — {idle_hosted_total} idle-hosted row(s) drain via "
            "the held-wake path (stalls on pre-fix runner code)"
        )
    else:
        lines.append("  → verdict: ready")
    return lines


def print_cohort_readiness(*, stream: IO[str] | None = None) -> None:
    """Print the report; never raises — a dry run must survive a database outage."""
    out = stream if stream is not None else sys.stdout
    try:
        targets = collect_targets()
    except Exception as exc:  # the report annotates a dry run; it cannot fail it
        print(f"→ per-target cohort readiness: skipped ({type(exc).__name__}: {exc})", file=out)
        return
    for line in report_lines(targets):
        print(line, file=out)
