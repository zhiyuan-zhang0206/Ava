"""One applied-termination observer reused by recovery and explicit resurrection."""

from typing import Any, cast

import psycopg
from psycopg.pq import TransactionStatus

from shared.lifecycle_process_identity import target_process_ended


def observe_applied_termination(conn: psycopg.Connection, agent_id: int, machine: str) -> bool:
    """Clear only the current applied command after its exact process has ended.

    True means there is no outstanding pointer, or this observer discharged it.
    It does not authorize admission itself. Unknown, live, hosted, non-terminate or
    changed-target commands remain untouched. Metadata precedes inbound locking.
    """
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("termination observation requires an explicit transaction")
    row = conn.execute(
        "SELECT runtime_generation,runtime_owner,runtime_kind,lifecycle_command_id,"
        "status,machine FROM agents_meta WHERE id=%s FOR UPDATE",
        (agent_id,),
    ).fetchone()
    if row is None:
        return False
    if row[3] is None:
        return True
    if row[2] != "process" or row[4:] != ("terminated", machine):
        return False
    command = conn.execute(
        "SELECT payload FROM inbound_messages WHERE id=%s AND agent_id=%s "
        "AND kind='terminate' AND status='claimed' AND applied_at IS NOT NULL "
        "AND observed_at IS NULL AND target_generation=%s AND target_owner=%s FOR UPDATE",
        (row[3], agent_id, row[0], row[1]),
    ).fetchone()
    if command is None or not isinstance(command[0], dict):
        return False
    if not target_process_ended(cast(dict[str, Any], command[0]), machine):
        return False
    observed = conn.execute(
        "UPDATE inbound_messages SET observed_at=clock_timestamp(),status='done' WHERE id=%s "
        "AND agent_id=%s AND status='claimed' AND target_generation=%s AND target_owner=%s",
        (row[3], agent_id, row[0], row[1]),
    )
    cleared = conn.execute(
        "UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s "
        "AND lifecycle_command_id=%s AND runtime_generation=%s AND runtime_owner=%s",
        (agent_id, row[3], row[0], row[1]),
    )
    if observed.rowcount != 1 or cleared.rowcount != 1:
        raise RuntimeError("termination observation lost its locked target")
    return True
