"""Durable budget for one queued user wake waiting for an old termination.

The existing resurrection caller owns retries. This module neither polls nor
launches: it binds that caller to one inbound and the old command's target.
"""

from typing import Any, cast

import psycopg
from psycopg.types.json import Jsonb

from shared.agents import AgentNotFound, MachinePaused, ResurrectAlreadyAlive, ResurrectError
from shared.boot_timing import BOOT_REAP_GRACE_SEC
from shared.db_transaction import write_transaction
from shared.machine import machine_name


class ResurrectExitDeferredError(ResurrectError):
    """The old execution entity has not yet been positively observed ended."""


class ResurrectTriggerStaleError(ResurrectError):
    """The exact pending wake no longer qualifies; the local op returns a no-op."""


def lock_active_home_machine(cur: psycopg.Cursor, agent_id: int) -> str:
    """Share the pause latch before metadata/inbound locks or budget writes."""
    cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
    agent_row = cur.fetchone()
    if agent_row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    home_machine = agent_row[0]
    if not isinstance(home_machine, str):
        raise ResurrectTriggerStaleError("resurrection target has no registered placement")
    cur.execute("SELECT paused_at FROM machines WHERE name = %s FOR SHARE", (home_machine,))
    machine_row = cur.fetchone()
    if machine_row is not None and machine_row[0] is not None:
        raise MachinePaused(
            f"agent {agent_id} home machine {home_machine!r} is paused; "
            "resume it before resurrecting"
        )
    return home_machine


def validate_pending_retry(conn: psycopg.Connection, agent_id: int, inbound_id: int) -> None:
    """Recheck the spent authorization inside the actual preparation row lock."""
    row = conn.execute(
        "SELECT payload FROM inbound_messages WHERE id=%s AND agent_id=%s FOR UPDATE",
        (inbound_id, agent_id),
    ).fetchone()
    if row is None:
        raise ResurrectTriggerStaleError("resurrection trigger is no longer pending")
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
        with conn.cursor() as cur:
            latched_machine = lock_active_home_machine(cur, agent_id)
        owner = conn.execute(
            "SELECT runtime_generation,runtime_owner,lifecycle_command_id,status,machine "
            "FROM agents_meta WHERE id=%s FOR UPDATE",
            (agent_id,),
        ).fetchone()
        if owner is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        if owner[4] != latched_machine:
            raise ResurrectTriggerStaleError("resurrection placement changed after pause latch")
        if owner[:3] == (None, None, None):
            # No owned termination/wait budget: the original preparation gate
            # still owns placement, pause and exact pending-work diagnostics.
            return
        if owner[3] != "terminated":
            raise ResurrectAlreadyAlive("pending resurrection target is no longer terminated")
        if owner[4] != machine_name():
            raise ResurrectTriggerStaleError("pending resurrection target is not local")
        inbound = conn.execute(
            "SELECT payload,created_at FROM inbound_messages WHERE id=%s AND agent_id=%s "
            "AND kind=%s AND status='pending' FOR UPDATE",
            (inbound_id, agent_id, kind),
        ).fetchone()
        if inbound is None:
            raise ResurrectTriggerStaleError("resurrection trigger is no longer pending")
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
