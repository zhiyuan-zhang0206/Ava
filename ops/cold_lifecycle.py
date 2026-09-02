"""Explicit cold lifecycle commands share the runtime acceptance writer.

Historical failures prove absence only; they never trigger resurrection. The
caller holds the exact agent row lock and supplies positive OS absence evidence.
"""

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from shared.lifecycle_acceptance import accept_lifecycle_command
from shared.runtime_incarnation import RuntimeIncarnation


def prepared_target_was_released(conn: psycopg.Connection, owner: dict[str, Any]) -> bool:
    """A failed prepared restart is durable proof, not NULL PID inference."""
    if owner["pid"] is not None or owner["lease_expires_at"] is not None:
        return False
    return conn.execute(
        "SELECT EXISTS(SELECT 1 FROM inbound_messages WHERE agent_id=%s "
        "AND target_generation=%s AND target_owner=%s AND kind='restart' AND status='done' "
        "AND applied_at IS NOT NULL AND observed_at IS NULL "
        "AND payload->'lifecycle_result'->>'outcome'='failed' "
        "AND payload->'lifecycle_result'->>'reason'='restart_deadline_expired')",
        (owner["id"], owner["runtime_generation"], owner["runtime_owner"]),
    ).fetchone() == (True,)


def fail_expired_restart(
    conn: psycopg.Connection,
    owner: dict[str, Any],
    command: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    """Caller proved original deadline elapsed and no admitted successor exists."""
    payload["lifecycle_result"] = {"outcome": "failed", "reason": "restart_deadline_expired"}
    changed = conn.execute(
        "UPDATE inbound_messages SET status='done',payload=%s WHERE id=%s AND agent_id=%s "
        "AND status='claimed' AND observed_at IS NULL",
        (Jsonb(payload), command["id"], owner["id"]),
    )
    if changed.rowcount != 1:
        raise RuntimeError("expired restart no longer owns its command")
    released = conn.execute(
        "UPDATE agents_meta SET lifecycle_command_id=NULL,status='terminated',termination_source='exit',pid=NULL, "
        "started_at=NULL,lease_expires_at=NULL WHERE id=%s AND lifecycle_command_id=%s "
        "AND runtime_generation=%s AND runtime_owner=%s",
        (owner["id"], command["id"], command["target_generation"], command["target_owner"]),
    )
    if released.rowcount != 1:
        raise RuntimeError("expired restart no longer owns its target pointer")


def accept_cold_command(conn: psycopg.Connection, owner: dict[str, Any]) -> int | None:
    """Accept only a new explicit lifecycle request after verified absence.

    Reuses precisely the live-runtime acceptance SQL; the controller does not
    invent another queue, claim timestamp or target-selection rule.
    """
    target = RuntimeIncarnation(owner["id"], owner["runtime_generation"], owner["runtime_owner"])
    command = accept_lifecycle_command(conn, target)
    if command is None:
        return None
    if command.kind == "restart":
        conn.execute("UPDATE agents_meta SET status='restarting' WHERE id=%s", (owner["id"],))
    elif command.kind == "terminate":
        conn.execute(
            "UPDATE agents_meta SET status='terminated',termination_source='user' WHERE id=%s",
            (owner["id"],),
        )
    else:
        raise ValueError(f"not a cold lifecycle command: {command.kind}")
    conn.execute(
        "UPDATE inbound_messages SET applied_at=clock_timestamp() WHERE id=%s AND applied_at IS NULL",
        (command.id,),
    )
    return command.id
