"""Exact local receipt recovery through existing admission/wake paths.

Never scan for the latest receipt: the complete database set chooses each exact
request directory. Missing, malformed, unattached or still-live identities refuse.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil
from psycopg import Connection

from base.agents.incarnation.exec_owner_protocol import (
    OwnerClosed,
    read_owner_bytes,
    read_owner_context,
)
from base.agents.incarnation.resources import (
    ExecAllocation,
    IncarnationResources,
    ResourceEvidenceError,
    ResourceProcess,
    ResourceShapeError,
    complete_exec,
    decode_resources,
)
from base.db import Database
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.paths import exec_run_dir


def process_ended(identity: ResourceProcess) -> bool:
    try:
        # Missing native evidence cannot become exit merely because a PID is
        # absent. Only a complete receipt can establish reuse or another boot.
        if not identity.in_current_boot():
            return True
        # PID reuse means the exact old process ended, not permission to signal
        # the replacement. This helper never signals or scans descendants.
        return not identity.as_identity().live()
    except (psutil.AccessDenied, OSError, RuntimeError, ValueError):
        return False


def _recoverable(value: object) -> object:
    """The decoded set, or None for a retired shape: it has no exact local
    evidence to recover, and admission and resurrection refuse it."""
    try:
        return decode_resources(value)
    except ResourceShapeError:
        return None


def _load_recoverable(
    db: Database, agent_id: int, machine: str
) -> tuple[object, IncarnationResources, RuntimeIncarnation] | None:
    """The agent's raw resources, decoded state and incarnation when this host may recover them."""
    with db.write_transaction() as conn:
        row = conn.execute(
            "SELECT incarnation_resources,runtime_generation,runtime_owner,machine FROM agents_meta WHERE id=%s",
            (agent_id,),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    state = _recoverable(row[0])
    if not isinstance(state, IncarnationResources) or row[3] != machine:
        return None
    target = RuntimeIncarnation(agent_id, state.generation, state.owner)
    if row[1:3] != (target.generation, target.owner):
        return None
    return row[0], state, target


def _verify_closed_receipt(
    target: RuntimeIncarnation, allocation: ExecAllocation, directory: Path
) -> bool:
    """True when the exec owner closed with a receipt exactly matching `allocation`.

    False when the owner left no context or receipt yet; a receipt that differs raises.
    """
    try:
        context = read_owner_context(directory / "owner.json")
        receipt = OwnerClosed.model_validate_json(read_owner_bytes(directory / "owner.closed"))
    except FileNotFoundError:
        return False
    if (
        (context.agent_id, context.generation, context.runtime_owner)
        != (target.agent_id, target.generation, target.owner)
        or context.allocation
        != allocation.model_copy(update={"owner_process": None, "root_process": None})
        or receipt.allocation != allocation
        or receipt.observed_at.tzinfo is None
        or receipt.observed_at > datetime.now(UTC)
        or context.request_path.parent != directory
        or hashlib.sha256(read_owner_bytes(context.request_path, 64 * 1024 * 1024)).hexdigest()
        != allocation.request_digest
    ):
        raise ResourceEvidenceError("recovery receipt differs from exact local allocation")
    return True


def _completed_allocations(
    agent_id: int, target: RuntimeIncarnation, state: IncarnationResources
) -> list[ExecAllocation]:
    """Exec allocations whose owner process ended and left a matching close receipt."""
    completed: list[ExecAllocation] = []
    for allocation in state.requests.values():
        if allocation.owner_process is None or not process_ended(allocation.owner_process):
            continue
        directory = (exec_run_dir() / str(agent_id) / "domains" / str(allocation.request)).resolve()
        if _verify_closed_receipt(target, allocation, directory):
            completed.append(allocation)
    return completed


def _settle_frozen_terminate(
    conn: Connection[Any], agent_id: int, target: RuntimeIncarnation, frozen_by: int
) -> None:
    """Close the terminate command that froze this incarnation and release its lifecycle lock."""
    command = conn.execute(
        "UPDATE inbound_messages SET status='done',observed_at=clock_timestamp() WHERE id=%s AND agent_id=%s AND kind='terminate' AND status='claimed' AND applied_at IS NOT NULL AND observed_at IS NULL AND target_generation=%s AND target_owner=%s RETURNING id",
        (frozen_by, agent_id, target.generation, target.owner),
    ).fetchone()
    if command is not None:
        conn.execute(
            "UPDATE agents_meta SET lifecycle_command_id=NULL,lease_expires_at=NULL WHERE id=%s AND lifecycle_command_id=%s",
            (agent_id, frozen_by),
        )


def recover_local_resources(db: Database, agent_id: int, machine: str) -> None:
    loaded = _load_recoverable(db, agent_id, machine)
    if loaded is None:
        return
    raw_resources, state, target = loaded
    completed = _completed_allocations(agent_id, target, state)
    host_ended = state.host_process is not None and process_ended(state.host_process)
    with db.write_transaction() as conn:
        current = conn.execute(
            "SELECT incarnation_resources,machine,runtime_generation,runtime_owner,lifecycle_command_id FROM agents_meta WHERE id=%s FOR UPDATE",
            (agent_id,),
        ).fetchone()
        if (
            current is None
            or current[0] != raw_resources
            or current[1:4] != (machine, target.generation, target.owner)
        ):
            return
        for allocation in completed:
            complete_exec(conn, target, allocation)
        if (
            not host_ended
            or len(completed) != len(state.requests)
            or state.frozen_by is None
            or current[4] != state.frozen_by
        ):
            return
        _settle_frozen_terminate(conn, agent_id, target, state.frozen_by)
