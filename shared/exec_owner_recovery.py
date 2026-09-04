"""Exact local receipt recovery through existing admission/wake paths.

Never scan for the latest receipt: the complete database set chooses each exact
request directory. Missing, malformed, unattached or still-live identities refuse.
"""

import hashlib
from datetime import UTC, datetime

import psutil

from shared.db_transaction import write_transaction
from shared.exec_owner_protocol import OwnerClosed, read_owner_bytes, read_owner_context
from shared.incarnation_resources import (
    ExecAllocation,
    IncarnationResources,
    ResourceEvidenceError,
    ResourceProcess,
    complete_exec,
    decode_resources,
)
from shared.paths import exec_run_dir
from shared.runtime_incarnation import RuntimeIncarnation


def process_ended(identity: ResourceProcess) -> bool:
    try:
        process = psutil.Process(identity.pid)
        # PID reuse means the exact old process ended, not permission to signal
        # the replacement. This helper never signals or scans descendants.
        return process.create_time() != identity.birth or process.status() in {
            psutil.STATUS_DEAD,
            psutil.STATUS_ZOMBIE,
        }
    except psutil.NoSuchProcess:
        return True
    except psutil.AccessDenied:
        return False


def recover_local_resources(agent_id: int, machine: str) -> None:
    with write_transaction() as conn:
        row = conn.execute(
            "SELECT incarnation_resources,runtime_generation,runtime_owner,machine FROM agents_meta WHERE id=%s",
            (agent_id,),
        ).fetchone()
    if row is None or row[0] is None:
        return
    state = decode_resources(row[0])
    if not isinstance(state, IncarnationResources) or row[3] != machine:
        return
    target = RuntimeIncarnation(agent_id, state.generation, state.owner)
    if row[1:3] != (target.generation, target.owner):
        return
    completed: list[ExecAllocation] = []
    for allocation in state.requests.values():
        if allocation.owner_process is None or not process_ended(allocation.owner_process):
            continue
        directory = (exec_run_dir() / str(agent_id) / "domains" / str(allocation.request)).resolve()
        try:
            context = read_owner_context(directory / "owner.json")
            receipt = OwnerClosed.model_validate_json(read_owner_bytes(directory / "owner.closed"))
        except FileNotFoundError:
            continue
        if (
            (context.agent_id, context.generation, context.runtime_owner)
            != (agent_id, target.generation, target.owner)
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
        completed.append(allocation)
    host_ended = state.host_process is not None and process_ended(state.host_process)
    with write_transaction() as conn:
        current = conn.execute(
            "SELECT incarnation_resources,machine,runtime_generation,runtime_owner,lifecycle_command_id FROM agents_meta WHERE id=%s FOR UPDATE",
            (agent_id,),
        ).fetchone()
        if (
            current is None
            or current[0] != row[0]
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
        command = conn.execute(
            "UPDATE inbound_messages SET status='done',observed_at=clock_timestamp() WHERE id=%s AND agent_id=%s AND kind='terminate' AND status='claimed' AND applied_at IS NOT NULL AND observed_at IS NULL AND target_generation=%s AND target_owner=%s RETURNING id",
            (state.frozen_by, agent_id, target.generation, target.owner),
        ).fetchone()
        if command is not None:
            conn.execute(
                "UPDATE agents_meta SET lifecycle_command_id=NULL,lease_expires_at=NULL WHERE id=%s AND lifecycle_command_id=%s",
                (agent_id, state.frozen_by),
            )
