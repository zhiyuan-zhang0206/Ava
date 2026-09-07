"""Durable restart cohort for an already admitted hosted runner.

Preparation and admission serialize on the existing agents_meta row lock.
The local journal is published first; a native admission that obtains its row
after publication must either belong to this cohort's original host owner or
refuse. No lifecycle target, acknowledgement, or checkpoint is invented here.
"""

from datetime import datetime
from typing import NamedTuple
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from shared import maintenance, pause_owner
from shared.maintenance_state import MaintenanceHold


def prepare(
    conn: psycopg.Connection,
    *,
    machine: str,
    host_owner: UUID | None,
    holder: str,
    acquired_at: datetime,
    host_absent: bool = False,
) -> MaintenanceHold:
    """Freeze the original runnable set and enqueue one restart per member.

    A failure leaves the hold in place. Repeating preparation resumes the
    same cohort and finds already committed commands by the exact operation.
    Terminated agents never enter the cohort. A previous lifecycle operation,
    stale/unknown runtime requires separate resolution.
    """
    current = maintenance.require_operation(holder, acquired_at)
    hold = current.maintenance
    assert hold is not None  # noqa: S101
    if hold.failures:
        raise RuntimeError(f"maintenance has failed continuations: {sorted(hold.failures)}")
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
        applied = _applied_capture(conn, hold, host_owner, holder, acquired_at)
        captured = _classify(
            [_RuntimeRow(*row) for row in rows], hold, host_owner, applied, host_absent=host_absent
        )
        cold = frozenset(
            row[0] for row in rows if host_absent and _RuntimeRow(*row).cold_hosted_idle()
        )
        _require_resolved(conn, captured, cold=cold)
        if captured != hold:
            pause_owner.change_maintenance(holder, acquired_at, hold, captured)
            hold = captured
        commands: dict[int, int] = {}
        for agent_id in sorted(hold.commands):
            commands[agent_id] = _restart(conn, agent_id, holder, acquired_at)
    draining = MaintenanceHold("draining", commands, parked=hold.parked)
    pause_owner.change_maintenance(holder, acquired_at, hold, draining)
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
        # release_hosted_owner clears the lease only after normal host cleanup.
        # A merely expired lease does not prove that boundary.
        return (
            self.status == "idling"
            and self.kind in (None, "hosted")
            and self.fresh is None
            and self.pid is None
            and self.resources is None
            and (self.owner is None) == (self.generation is None)
        )

    def active_for(self, owner: UUID | None) -> bool:
        return (
            self.status in ("running", "idling")
            and self.kind == "hosted"
            and self.owner == owner
            and self.generation is not None
            and self.fresh is True
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
        raise RuntimeError(f"maintenance requires the live original native owner: {invalid}")
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
    unresolved = conn.execute(
        "SELECT DISTINCT agent_id FROM inbound_messages WHERE agent_id=ANY(%s) "
        "AND ((status='claimed' AND (NOT (agent_id=ANY(%s)) OR kind='terminate' "
        "OR (kind='restart' AND applied_at IS NULL))) "
        "OR (status='pending' AND kind IN ('restart','terminate')))",
        (list(hold.parked), list(cold)),
    ).fetchall()
    if unresolved:
        raise RuntimeError(
            f"parked agents have unresolved claimed work: {[row[0] for row in unresolved]}"
        )


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
    """Cross-check local continuation receipts against preserved DB pointers."""
    if hold.failures or set(hold.drained) != set(hold.commands):
        raise RuntimeError("maintenance still has unfinished or failed continuations")
    if hold.parked:
        rows = conn.execute(
            "SELECT id FROM agents_meta WHERE id=ANY(%s) AND status='idling' "
            "AND ((runtime_owner IS NULL AND runtime_generation IS NULL) OR "
            "(runtime_kind='hosted' AND lease_expires_at IS NULL)) "
            "AND pid IS NULL AND incarnation_resources IS NULL",
            (list(hold.parked),),
        ).fetchall()
        if {row[0] for row in rows} != set(hold.parked):
            raise RuntimeError("parked agent intent changed during maintenance")
    for agent_id, command_id in hold.commands.items():
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
