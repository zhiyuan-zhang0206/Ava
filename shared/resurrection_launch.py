"""The existing resurrect inbound is the authority for its bounded OS attempts.

Callers lock agents_meta before these helpers. Authorization commits before
launch; the child validates the same record before admission. No session record
or controller-local retry counter grants execution authority.
"""

from datetime import datetime
from typing import Any, cast

import psycopg
from psycopg.types.json import Jsonb

from shared.agents import ResurrectError
from shared.boot_timing import BOOT_REAP_GRACE_SEC


def prepare_launch(
    conn: psycopg.Connection,
    agent_id: int,
    command_id: int,
    allocation_epoch: datetime,
    trigger_id: int | None,
) -> None:
    """Bind the new marker to this allocation; never copy a wake's retry count."""
    deadline = conn.execute(
        "SELECT created_at+make_interval(secs=>%s) FROM inbound_messages WHERE id=%s "
        "AND agent_id=%s",
        (BOOT_REAP_GRACE_SEC, trigger_id if trigger_id is not None else command_id, agent_id),
    ).fetchone()
    if deadline is None:
        raise ResurrectError("resurrection deadline source disappeared")
    owner = conn.execute(
        "SELECT runtime_generation,runtime_owner,machine FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone()
    if owner is None:
        raise ResurrectError("resurrection allocation disappeared")
    payload = {
        "resurrection_launch": {
            "allocation_epoch": allocation_epoch.isoformat(),
            "trigger_id": trigger_id,
            "deadline": deadline[0].isoformat(),
            "attempts": 0,
            "target_generation": str(owner[0]) if owner[0] is not None else None,
            "target_owner": str(owner[1]) if owner[1] is not None else None,
            "machine": owner[2],
        }
    }
    conn.execute(
        "UPDATE inbound_messages i SET payload=%s FROM agents_meta m "
        "WHERE m.id=%s AND i.agent_id=m.id "
        "AND i.id=%s AND i.kind='resurrect' AND i.status='pending'",
        (Jsonb(payload), agent_id, command_id),
    )


def _launch_state(raw: object) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(raw, dict):
        raise TypeError("resurrection launch payload must be an object")
    payload = cast(dict[str, Any], raw)
    state: object = payload["resurrection_launch"]
    if not isinstance(state, dict):
        raise TypeError("resurrection launch evidence must be an object")
    launch = cast(dict[str, Any], state)
    if set(launch) != {
        "allocation_epoch",
        "trigger_id",
        "deadline",
        "attempts",
        "target_generation",
        "target_owner",
        "machine",
    }:
        raise ValueError("invalid resurrection launch fields")
    if type(launch["attempts"]) is not int or launch["attempts"] < 0:
        raise ValueError("invalid resurrection launch counter")
    if launch["trigger_id"] is not None and (
        type(launch["trigger_id"]) is not int or launch["trigger_id"] <= 0
    ):
        raise ValueError("invalid resurrection trigger identity")
    return payload, launch


def require_launch(
    conn: psycopg.Connection, agent_id: int, command_id: int
) -> tuple[dict[str, Any], dict[str, Any], float]:
    """Validate after the metadata lock, with a fresh clock after inbound lock."""
    row = conn.execute(
        "SELECT payload FROM inbound_messages WHERE id=%s AND agent_id=%s "
        "AND kind='resurrect' AND status='pending' FOR UPDATE",
        (command_id, agent_id),
    ).fetchone()
    if row is None:
        raise ResurrectError("resurrection launch is no longer pending")
    payload, launch = _launch_state(row[0])
    valid = conn.execute(
        "SELECT extract(epoch FROM (%s::timestamptz-clock_timestamp())) "
        "FROM agents_meta m JOIN inbound_messages i ON i.agent_id=m.id "
        "WHERE m.id=%s AND i.id=%s AND m.status='idling' AND m.pid IS NULL "
        "AND m.status_changed_at=%s::timestamptz AND m.lifecycle_command_id IS NULL "
        "AND m.runtime_generation IS NOT DISTINCT FROM %s::uuid "
        "AND m.runtime_owner IS NOT DISTINCT FROM %s::uuid AND m.machine=%s",
        (
            launch["deadline"],
            agent_id,
            command_id,
            launch["allocation_epoch"],
            launch["target_generation"],
            launch["target_owner"],
            launch["machine"],
        ),
    ).fetchone()
    if valid is None or float(valid[0]) <= 0:
        raise ResurrectError("resurrection launch allocation changed or deadline expired")
    return payload, launch, float(valid[0])


def authorize_launch(
    conn: psycopg.Connection, agent_id: int, command_id: int, limit: int
) -> tuple[int, float]:
    """Spend once in the caller's short transaction, including pre-spawn crashes."""
    payload, launch, remaining = require_launch(conn, agent_id, command_id)
    attempts = launch["attempts"]
    trigger_id = launch["trigger_id"]
    if trigger_id is not None:
        wake = conn.execute(
            "SELECT payload FROM inbound_messages WHERE id=%s AND agent_id=%s "
            "AND status='pending' FOR UPDATE",
            (trigger_id, agent_id),
        ).fetchone()
        if wake is None or (wake[0] is not None and not isinstance(wake[0], dict)):
            raise ResurrectError("resurrection wake no longer pending")
        wake_payload = cast(dict[str, Any], wake[0] or {})
        attempts = wake_payload.get("resurrection_launch_attempts", 0)
        if type(attempts) is not int or attempts < 0:
            raise ValueError("invalid persisted wake launch counter")
        if attempts >= limit:
            raise ResurrectError("pending wake OS launch budget exhausted")
        wake_payload["resurrection_launch_attempts"] = attempts + 1
        conn.execute(
            "UPDATE inbound_messages SET payload=%s WHERE id=%s", (Jsonb(wake_payload), trigger_id)
        )
    if attempts >= limit:
        raise ResurrectError("resurrection OS launch budget exhausted")
    # Lock waits above must not extend the original deadline.
    _, _, remaining = require_launch(conn, agent_id, command_id)
    launch["attempts"] = attempts + 1
    conn.execute("UPDATE inbound_messages SET payload=%s WHERE id=%s", (Jsonb(payload), command_id))
    return attempts + 1, remaining


def require_admission(conn: psycopg.Connection, agent_id: int, command_id: int | None) -> None:
    """An unlabelled or delayed boot cannot inherit another launch allocation."""
    if command_id is None:
        guarded = conn.execute(
            "SELECT 1 FROM inbound_messages i JOIN agents_meta m ON m.id=i.agent_id "
            "WHERE m.id=%s AND i.kind='resurrect' AND i.status='pending' "
            "AND (i.payload->'resurrection_launch'->>'allocation_epoch')::timestamptz"
            "=m.status_changed_at",
            (agent_id,),
        ).fetchone()
        if guarded is not None:
            raise ResurrectError("resurrection admission requires its launch identity")
        return
    _, launch, _ = require_launch(conn, agent_id, command_id)
    if launch["attempts"] <= 0:
        raise ResurrectError("resurrection admission lacks committed OS authorization")
