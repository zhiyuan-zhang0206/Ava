"""Durable budget for one queued user wake waiting for an old termination.

The existing resurrection caller owns retries. This module neither polls nor
launches: it binds that caller to one inbound and the old command's target.
"""

from typing import Any, cast

import psycopg
from psycopg.types.json import Jsonb

from shared.agents import ResurrectError
from shared.boot_timing import BOOT_REAP_GRACE_SEC
from shared.db_transaction import write_transaction
from shared.machine import machine_name


class ResurrectExitDeferredError(ResurrectError):
    """The old execution entity has not yet been positively observed ended."""


def validate_pending_retry(conn: psycopg.Connection, agent_id: int, inbound_id: int) -> None:
    """Recheck the spent authorization inside the actual preparation row lock."""
    row = conn.execute(
        "SELECT payload FROM inbound_messages WHERE id=%s AND agent_id=%s "
        "AND status='pending' FOR UPDATE",
        (inbound_id, agent_id),
    ).fetchone()
    if row is None:
        raise ResurrectError("resurrection trigger is no longer pending")
    raw: object = row[0] or {}
    if not isinstance(raw, dict):
        raise TypeError("resurrection trigger payload must be an object")
    payload = cast(dict[str, Any], raw)
    state: object = payload.get("resurrection_retry")
    if state is None:
        return
    if not isinstance(state, dict):
        raise TypeError("invalid reserved resurrection retry evidence")
    retry = cast(dict[str, Any], state)
    blocked_by = retry["blocked_by"]
    if type(blocked_by) is not int or blocked_by <= 0:
        raise ValueError("invalid resurrection blocker")
    verified = conn.execute(
        "SELECT 1 FROM agents_meta m JOIN inbound_messages old ON old.agent_id=m.id "
        "JOIN inbound_messages wake ON wake.agent_id=m.id "
        "WHERE m.id=%s AND m.status='terminated' AND m.machine=%s "
        "AND old.id=%s AND old.kind='terminate' AND old.applied_at IS NOT NULL "
        "AND old.target_generation=m.runtime_generation AND old.target_owner=m.runtime_owner "
        "AND (m.lifecycle_command_id IS NULL OR m.lifecycle_command_id=old.id) "
        "AND wake.id=%s AND wake.status='pending' "
        "AND wake.created_at+make_interval(secs=>%s)>clock_timestamp()",
        (agent_id, machine_name(), blocked_by, inbound_id, BOOT_REAP_GRACE_SEC),
    ).fetchone()
    if verified is None:
        raise ResurrectError("pending resurrection authorization expired or changed target")


def authorize_pending_retry(agent_id: int, inbound_id: int, kind: str, limit: int) -> None:
    """Spend a fixed request's attempt before preparing, never reset on redispatch.

    Only the old applied terminate creates this budget. Target facts are read
    from that existing command, not duplicated into the chat payload. The count
    is an authorized preparation attempt, not an OS spawn count: waiting for an
    old exit consumes one of the existing caller's attempts too. It cannot reset
    or expand the separate launch policy. Exhaustion leaves the chat pending.
    """
    with write_transaction() as conn:
        owner = conn.execute(
            "SELECT runtime_generation,runtime_owner,lifecycle_command_id,status,machine "
            "FROM agents_meta WHERE id=%s FOR UPDATE",
            (agent_id,),
        ).fetchone()
        if owner is None or owner[3:] != ("terminated", machine_name()):
            raise ResurrectError("pending resurrection no longer targets this local termination")
        inbound = conn.execute(
            "SELECT payload,created_at FROM inbound_messages WHERE id=%s AND agent_id=%s "
            "AND kind=%s AND status='pending' FOR UPDATE",
            (inbound_id, agent_id, kind),
        ).fetchone()
        if inbound is None:
            raise ResurrectError("resurrection trigger is no longer pending")
        raw: object = inbound[0] or {}
        if not isinstance(raw, dict):
            raise TypeError("resurrection trigger payload must be an object")
        payload = cast(dict[str, Any], raw)
        retry: object = payload.get("resurrection_retry")
        if retry is None and owner[2] is None:
            return
        if retry is None:
            blocked_by, attempts = owner[2], 0
        else:
            if not isinstance(retry, dict):
                raise ValueError("invalid reserved resurrection retry evidence")
            state = cast(dict[str, Any], retry)
            if set(state) != {"blocked_by", "attempts"}:
                raise ValueError("invalid reserved resurrection retry fields")
            blocked_by, attempts = state["blocked_by"], state["attempts"]
        if (
            type(blocked_by) is not int
            or blocked_by <= 0
            or type(attempts) is not int
            or attempts < 0
        ):
            raise ValueError("invalid reserved resurrection retry identity or counter")
        if owner[2] not in (None, blocked_by):
            raise ResurrectError("a different lifecycle command now owns the target")
        target = conn.execute(
            "SELECT 1 FROM inbound_messages WHERE id=%s AND agent_id=%s AND kind='terminate' "
            "AND applied_at IS NOT NULL AND target_generation=%s AND target_owner=%s",
            (blocked_by, agent_id, owner[0], owner[1]),
        ).fetchone()
        if target is None:
            raise ResurrectError("resurrection retry target incarnation changed or is unknown")
        fresh = conn.execute(
            "SELECT %s+make_interval(secs=>%s)>clock_timestamp()",
            (inbound[1], BOOT_REAP_GRACE_SEC),
        ).fetchone()
        if attempts >= limit or fresh != (True,):
            raise ResurrectError(
                f"inbound {inbound_id} remains pending: resurrection exit-wait budget exhausted"
            )
        payload["resurrection_retry"] = {"blocked_by": blocked_by, "attempts": attempts + 1}
        conn.execute(
            "UPDATE inbound_messages SET payload=%s WHERE id=%s", (Jsonb(payload), inbound_id)
        )
