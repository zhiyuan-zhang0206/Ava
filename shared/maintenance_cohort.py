"""Durable restart cohort for an already admitted hosted runner.

Preparation and admission serialize on the existing agents_meta row lock.
The local journal is published first; a native admission that obtains its row
after publication must either belong to this cohort's original host owner or
refuse. No lifecycle target, acknowledgement, or checkpoint is invented here.
"""

from datetime import datetime
from typing import Literal, NamedTuple
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from shared import maintenance, pause_owner, telemetry
from shared.config import settings
from shared.hold_driver import HoldDriver
from shared.maintenance_state import MaintenanceHold


class LifecycleCollisionError(RuntimeError):
    """Preparation met unfinished lifecycle work it did not author.

    Raised before the cohort is frozen (task #3591) so a bounded retry has a
    clean boundary: a collision leaves the journal untouched, and the retry
    re-derives the cohort from the resolved world under the same row locks.
    ``waitable`` is True only when every competing row carries no maintenance
    payload: an ordinary agent lifecycle operation (``restart``/``terminate``)
    and any parked claims left after orphan settlement enter the bounded wait;
    maintenance-authored commands keep their refuse-now semantics, and a mix
    refuses too.
    """

    def __init__(self, detail: str, agent_ids: list[int], *, waitable: bool) -> None:
        super().__init__(detail)
        self.agent_ids = tuple(agent_ids)
        self.waitable = waitable


def prepare(
    conn: psycopg.Connection,
    *,
    machine: str,
    host_owner: UUID | None,
    holder: str,
    acquired_at: datetime,
    host_absent: bool = False,
    driver: HoldDriver | None = None,
) -> MaintenanceHold:
    """Freeze the original runnable set and enqueue one restart per member.

    A failure leaves the hold in place. Repeating preparation resumes the
    same cohort and finds already committed commands by the exact operation.
    A captured cohort is returned unchanged when no unsettled failures remain.
    Terminated agents never enter the cohort. A previous lifecycle operation,
    stale/unknown runtime requires separate resolution. Ordinary orphan claims
    on non-cold parked agents settle before the collision guards run.

    An unfinished agent lifecycle command belonging to another actor raises
    ``LifecycleCollisionError`` before the cohort is frozen; the caller may
    bounded-wait and retry, and the retry re-verifies everything under the
    same row locks (task #3591).
    """
    current = maintenance.require_operation(holder, acquired_at)
    hold = current.maintenance
    assert hold is not None  # noqa: S101
    if unsettled := hold.unsettled_failures():
        raise RuntimeError(f"maintenance has failed continuations: {sorted(unsettled)}")
    if hold.phase != "preparing":
        return hold
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("maintenance preparation requires an idle connection it can commit")
    with conn.transaction():
        conn.execute("SET TRANSACTION READ WRITE")
        conn.execute("SET LOCAL lock_timeout='5s'")
        rows = conn.execute(
            "SELECT id,status,runtime_kind,runtime_owner,runtime_generation,"
            "lease_expires_at>clock_timestamp(),pid,incarnation_resources FROM agents_meta "
            "WHERE machine=%s AND status<>'terminated' ORDER BY id FOR UPDATE",
            (machine,),
        ).fetchall()
        if host_absent:
            from shared.maintenance_cold import normalize_retired_intent

            for index, values in enumerate(rows):
                row = _RuntimeRow(*values)
                if row.retired_intent():
                    normalize_retired_intent(
                        conn, row.agent_id, restarting=row.status == "restarting"
                    )
                    rows[index] = row._replace(status="idling")
        applied = _applied_capture(conn, hold, host_owner, holder, acquired_at)
        captured = _classify(
            [_RuntimeRow(*row) for row in rows], hold, host_owner, applied, host_absent=host_absent
        )
        cold = frozenset(
            row[0] for row in rows if host_absent and _RuntimeRow(*row).cold_hosted_idle()
        )
        settled = _settle_orphan_claims(conn, captured, cold)
        _refuse_inflight_lifecycle(conn, captured, cold, holder=holder, acquired_at=acquired_at)
        _require_resolved(conn, captured, cold=cold)
        if captured != hold:
            pause_owner.change_maintenance(
                holder, acquired_at, hold, captured, refresh_driver=driver is not None
            )
            hold = captured
        commands: dict[int, int] = {}
        for agent_id in sorted(hold.commands):
            commands[agent_id] = _restart(conn, agent_id, holder, acquired_at)
    _emit_orphan_settlements(settled)
    draining = MaintenanceHold("draining", commands, parked=hold.parked)
    pause_owner.change_maintenance(
        holder, acquired_at, hold, draining, refresh_driver=driver is not None
    )
    return draining


class _RuntimeRow(NamedTuple):
    agent_id: int
    status: str
    kind: str | None
    owner: UUID | None
    generation: UUID | None
    fresh: bool | None
    pid: int | None
    resources: object

    def unowned_idle(self) -> bool:
        return (
            self.status == "idling"
            and self.kind in (None, "hosted")
            and self.owner is None
            and self.generation is None
            and self.fresh is not True
            and self.pid is None
            and self.resources is None
        )

    def cold_hosted_idle(self) -> bool:
        # Expired owned rows enter here only after retired_intent's persisted
        # END and native-absence proof. Their historical lease stays unchanged.
        return (
            self.status == "idling"
            and self.kind in (None, "hosted")
            and (self.fresh is None or self.retired_intent())
            and self.pid is None
            and self.resources is None
            and (self.owner is None) == (self.generation is None)
        )

    def retired_intent(self) -> bool:
        return (
            self.status in ("idling", "restarting")
            and (self.status == "restarting" or self.fresh is False)
            and self.kind == "hosted"
            and self.owner is not None
            and self.generation is not None
            and self.fresh is not True
            and self.pid is None
            and self.resources is None
        )

    def active_for(self, owner: UUID | None) -> bool:
        return (
            self.status in ("running", "idling")
            and self.kind == "hosted"
            and self.owner == owner
            and self.generation is not None
            and self.fresh is True
        )


class OrphanClaim(NamedTuple):
    """An ordinary claimed inbound on a parked, runtime-less agent."""

    agent_id: int
    message_id: int
    age_s: float


def orphaned_claims(
    conn: psycopg.Connection,
    *,
    parked: tuple[int, ...] | None = None,
    cold: frozenset[int] = frozenset(),
) -> list[OrphanClaim]:
    """Read the ordinary claims eligible for preparation's orphan settlement.

    Preparation supplies its locked, classified parked set and excludes cold
    agents whose ordinary claims never block `_unresolved_parked`. Preflight
    omits the set for a read-only snapshot across machines, using the same
    unowned-idle predicate as `_classify`; that snapshot authorizes no writes.
    """
    if parked is None:
        rows = conn.execute(
            "SELECT id,status,runtime_kind,runtime_owner,runtime_generation,"
            "lease_expires_at>clock_timestamp(),pid,incarnation_resources FROM agents_meta "
            "WHERE status='idling' ORDER BY id"
        ).fetchall()
        parked = tuple(row[0] for row in rows if _RuntimeRow(*row).unowned_idle())
    agents = sorted(set(parked) - cold)
    if not agents:
        return []
    return [
        OrphanClaim(*row)
        for row in conn.execute(
            "SELECT agent_id,id,"
            "EXTRACT(EPOCH FROM (now()-COALESCE(claimed_at,created_at)))::double precision "
            "FROM inbound_messages WHERE agent_id=ANY(%s) AND status='claimed' "
            "AND payload->'maintenance' IS NULL AND kind NOT IN ('restart','terminate') "
            "ORDER BY agent_id,id",
            (agents,),
        ).fetchall()
    ]


def _settle_orphan_claims(
    conn: psycopg.Connection, hold: MaintenanceHold, cold: frozenset[int]
) -> list[tuple[OrphanClaim, Literal["pending", "done"]]]:
    """Settle under preparation's agent row locks; preserve boot's stale cutoff."""
    settled: list[tuple[OrphanClaim, Literal["pending", "done"]]] = []
    cutoff = settings.daemon.delivery_watchdog_stale_claimed_threshold_seconds
    for row in orphaned_claims(conn, parked=hold.parked, cold=cold):
        outcome = "done" if row.age_s > cutoff else "pending"
        changed = conn.execute(
            "UPDATE inbound_messages SET status=%s WHERE id=%s AND status='claimed'",
            (outcome, row.message_id),
        ).rowcount
        if changed:
            settled.append((row, outcome))
    return settled


def _emit_orphan_settlements(
    settled: list[tuple[OrphanClaim, Literal["pending", "done"]]],
) -> None:
    """Report only committed CAS changes, never a rolled-back preparation attempt."""
    for row, outcome in settled:
        telemetry.emit(
            "telemetry",
            "pause_orphan_claim_settled",
            agent_id=row.agent_id,
            attributes={
                "agent": row.agent_id,
                "message_id": row.message_id,
                "age_s": round(row.age_s, 3),
                "outcome": outcome,
            },
        )


def _classify(
    rows: list[_RuntimeRow],
    hold: MaintenanceHold,
    owner: UUID | None,
    applied: set[int],
    *,
    host_absent: bool = False,
) -> MaintenanceHold:
    parked = tuple(
        sorted(
            row.agent_id
            for row in rows
            if row.agent_id not in hold.commands
            and (row.unowned_idle() or (host_absent and row.cold_hosted_idle()))
        )
    )
    captured = bool(hold.commands or hold.parked)
    if captured and hold.parked != parked:
        raise RuntimeError("parked native intent changed while preparing")
    candidates = set(hold.commands) if captured else {row.agent_id for row in rows} - set(parked)
    selected = [row for row in rows if row.agent_id in candidates]
    if {row.agent_id for row in selected} != candidates:
        raise RuntimeError("maintenance cohort changed lifecycle while preparing")
    invalid = [
        row.agent_id
        for row in selected
        if not row.active_for(owner) and row.agent_id not in applied
    ]
    if invalid:
        stranded = [
            row.agent_id for row in rows if row.agent_id in invalid and row.status == "restarting"
        ]
        guidance = (
            " (a stranded update straggler-reap mark clears via `ava start` —"
            " settle + wake — or at the next agent-host boot)"
            if stranded
            else ""
        )
        raise RuntimeError(
            f"maintenance requires the live original native owner: {invalid}{guidance}"
        )
    return (
        hold
        if captured
        else MaintenanceHold(commands=dict.fromkeys(sorted(candidates), 0), parked=parked)
    )


def _applied_capture(
    conn: psycopg.Connection,
    hold: MaintenanceHold,
    owner: UUID | None,
    holder: str,
    acquired_at: datetime,
) -> set[int]:
    # The original host can apply a committed restart before the journal's
    # final write succeeds. Preserve this cohort on retry; this is NOT a final
    # continuation receipt, which only the original boot may later sign.
    operation = Jsonb({"holder": holder, "acquired_at": acquired_at.isoformat()})
    rows = conn.execute(
        "SELECT m.id FROM agents_meta m JOIN inbound_messages i "
        "ON i.id=m.lifecycle_command_id AND i.agent_id=m.id "
        "WHERE m.id=ANY(%s) AND m.status='idling' AND m.runtime_owner IS NULL "
        "AND m.runtime_generation IS NULL AND m.incarnation_resources IS NULL "
        "AND i.kind='restart' AND i.source='system:maintenance' AND i.status='claimed' "
        "AND i.target_owner=%s "
        "AND i.applied_at IS NOT NULL AND i.observed_at IS NULL "
        "AND i.payload->'maintenance'=%s",
        (list(hold.commands), owner, operation),
    ).fetchall()
    return {row[0] for row in rows}


def _require_resolved(
    conn: psycopg.Connection, hold: MaintenanceHold, *, cold: frozenset[int] = frozenset()
) -> None:
    """The second guard on the same collision, with the same waitability rule.

    ``_refuse_inflight_lifecycle`` runs first and already covers every parked
    row it finds unresolved; this repeats the parked half independently so a
    change to either query cannot silently skip parked agents (task #3591). It
    raises the same typed collision: parked rows without a maintenance payload
    enter the bounded wait; maintenance-authored rows refuse immediately — and
    preparation still never freezes past unresolved parked claims.
    """
    rows = [
        _CommandRow(*row)
        for row in conn.execute(
            "SELECT agent_id, id, kind, status, applied_at IS NOT NULL, payload->'maintenance' "
            "FROM inbound_messages WHERE agent_id=ANY(%s) AND status IN ('pending','claimed') "
            "ORDER BY agent_id, id",
            (list(hold.parked),),
        ).fetchall()
    ]
    unresolved = [row for row in rows if _unresolved_parked(row, row.agent_id, cold)]
    if not unresolved:
        return
    raise LifecycleCollisionError(
        "; ".join(_collision_line(row.agent_id, [row]) for row in unresolved),
        sorted({row.agent_id for row in unresolved}),
        waitable=all(row.maintenance is None for row in unresolved),
    )


class _CommandRow(NamedTuple):
    """One pending/claimed inbound command relevant to a collision decision."""

    agent_id: int
    command_id: int
    kind: str
    status: str
    applied: bool
    maintenance: object


def _refuse_inflight_lifecycle(
    conn: psycopg.Connection,
    hold: MaintenanceHold,
    cold: frozenset[int],
    *,
    holder: str,
    acquired_at: datetime,
) -> None:
    """Declare unfinished lifecycle work before the cohort freezes (task #3591).

    Runs inside preparation's row-locked transaction but BEFORE the capture is
    persisted: a ``LifecycleCollisionError`` then leaves the journal untouched, so
    the caller's bounded retry starts from the same clean boundary and
    re-verifies every condition under the same locks. This mirrors the two
    downstream refusal checks — the per-member pending-command check in
    ``_restart`` and the parked-agent predicate of ``_require_resolved`` — one
    step earlier, carrying the waitability the retry decision needs: ordinary
    work without a maintenance payload is waitable — both an in-flight agent
    lifecycle operation (restart/terminate) and any parked claim left after
    orphan settlement; maintenance-authored commands are not.
    """
    agents = sorted(set(hold.commands) | set(hold.parked))
    if not agents:
        return
    rows = [
        _CommandRow(*row)
        for row in conn.execute(
            "SELECT agent_id, id, kind, status, applied_at IS NOT NULL, payload->'maintenance' "
            "FROM inbound_messages WHERE agent_id=ANY(%s) AND status IN ('pending','claimed') "
            "ORDER BY agent_id, id",
            (agents,),
        ).fetchall()
    ]
    operation = {"holder": holder, "acquired_at": acquired_at.isoformat()}
    lines: list[str] = []
    blocked: list[int] = []
    waitable = True
    for agent_id in sorted(hold.commands):
        member = [
            row for row in rows if row.agent_id == agent_id and row.kind in ("restart", "terminate")
        ]
        if not member or (len(member) == 1 and member[0].maintenance == operation):
            continue
        lines.append(_collision_line(agent_id, member))
        blocked.append(agent_id)
        waitable = waitable and all(row.maintenance is None for row in member)
    for agent_id in sorted(hold.parked):
        for row in rows:
            if row.agent_id != agent_id or not _unresolved_parked(row, agent_id, cold):
                continue
            lines.append(_collision_line(agent_id, [row]))
            blocked.append(agent_id)
            waitable = waitable and row.maintenance is None
    if not lines:
        return
    raise LifecycleCollisionError("; ".join(lines), sorted(set(blocked)), waitable=waitable)


def _collision_line(agent_id: int, rows: list[_CommandRow]) -> str:
    detail = ", ".join(f"{row.kind} {row.command_id} ({row.status})" for row in rows)
    lifecycle = all(row.kind in ("restart", "terminate") for row in rows)
    problem = "another unfinished lifecycle command" if lifecycle else "unresolved claimed work"
    return f"agent {agent_id} has {problem}: {detail}"


def _unresolved_parked(row: _CommandRow, agent_id: int, cold: frozenset[int]) -> bool:
    """`_require_resolved`'s predicate, per row, for the waitability decision."""
    if row.status == "claimed":
        return (
            agent_id not in cold
            or row.kind == "terminate"
            or (row.kind == "restart" and not row.applied)
        )
    return row.kind in ("restart", "terminate")


def _restart(conn: psycopg.Connection, agent_id: int, holder: str, acquired_at: datetime) -> int:
    operation = {"holder": holder, "acquired_at": acquired_at.isoformat()}
    pending = conn.execute(
        "SELECT id,payload->'maintenance' FROM inbound_messages WHERE agent_id=%s "
        "AND kind IN ('restart','terminate') AND status IN ('pending','claimed') "
        "ORDER BY id FOR UPDATE",
        (agent_id,),
    ).fetchall()
    if pending:
        if len(pending) != 1 or pending[0][1] != operation:
            raise RuntimeError(f"agent {agent_id} has another unfinished lifecycle command")
        return pending[0][0]
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content,payload) "
        "VALUES(%s,'restart','system:maintenance','',%s) RETURNING id",
        (agent_id, Jsonb({"maintenance": operation})),
    ).fetchone()
    assert row is not None  # noqa: S101
    return row[0]


def verify_drained(conn: psycopg.Connection, hold: MaintenanceHold) -> None:
    """Cross-check local continuation receipts against preserved DB pointers.

    Reaped members (task #4016) have no flush/apply receipt by construction:
    their certification checks the honest reap state instead -- the row still
    CAS-marked 'restarting' and its command never applied or observed. A
    failure recorded around that release (the interrupted turn unwinding) is
    settled too: the reap left nothing to repair, so it is not read here.
    """
    if hold.unsettled_failures() or set(hold.drained) | set(hold.reaped) != set(hold.commands):
        raise RuntimeError("maintenance still has unfinished or failed continuations")
    if hold.parked:
        rows = conn.execute(
            "SELECT id FROM agents_meta WHERE id=ANY(%s) AND status='idling' "
            "AND ((runtime_owner IS NULL AND runtime_generation IS NULL) OR "
            "(runtime_kind='hosted' AND (lease_expires_at IS NULL "
            "OR lease_expires_at<=clock_timestamp()))) "
            "AND pid IS NULL AND incarnation_resources IS NULL",
            (list(hold.parked),),
        ).fetchall()
        if {row[0] for row in rows} != set(hold.parked):
            raise RuntimeError("parked agent intent changed during maintenance")
    for agent_id, command_id in hold.commands.items():
        if agent_id in hold.reaped:
            row = conn.execute(
                "SELECT 1 FROM agents_meta m JOIN inbound_messages i "
                "ON i.id=%s AND i.agent_id=m.id "
                "WHERE m.id=%s AND m.status='restarting' AND i.kind='restart' "
                "AND i.applied_at IS NULL AND i.observed_at IS NULL "
                "AND i.status IN ('pending','claimed')",
                (command_id, agent_id),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    f"reaped agent {agent_id} is not in its reap state; the mark moved"
                )
            continue
        row = conn.execute(
            "SELECT 1 FROM agents_meta m JOIN inbound_messages i "
            "ON i.id=m.lifecycle_command_id AND i.agent_id=m.id "
            "WHERE m.id=%s AND i.id=%s AND i.kind='restart' AND i.status='claimed' "
            "AND i.applied_at IS NOT NULL AND i.observed_at IS NULL "
            "AND m.status='idling' AND m.runtime_owner IS NULL AND m.runtime_generation IS NULL "
            "AND m.incarnation_resources IS NULL",
            (agent_id, command_id),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"agent {agent_id} no longer has its drained restart pointer")
