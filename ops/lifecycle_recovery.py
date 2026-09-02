"""Recover the fixed durable command through the existing restarter tick."""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ops import agent_launch
from ops.agent_identity import AgentProcessIdentity, probe_agent_process
from ops.cold_lifecycle import (
    accept_cold_command,
    fail_expired_restart,
    prepared_target_was_released,
)
from shared.boot_timing import BOOT_BUDGET_SEC
from shared.cluster import session_name
from shared.db_transaction import write_transaction
from shared.machine import machine_name
from shared.session_backend import native_proc

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RestartAttempt:
    command_id: int
    number: int
    remaining_budget: float
    config_overlay: dict[str, object] | None
    birth_config: dict[str, object] | None


def _attempt_count(payload: dict[str, object]) -> int:
    count = payload.get("launch_attempts", 0)
    if type(count) is not int or not 0 <= count <= agent_launch._LAUNCH_MAX_RETRIES + 1:
        raise ValueError("invalid reserved launch_attempts counter")
    return count


def _accept_if_absent(conn: Connection, owner: dict[str, Any]) -> int | None:
    pid = owner["pid"]
    absent = (
        probe_agent_process(pid, owner["id"])
        in {AgentProcessIdentity.GONE, AgentProcessIdentity.FOREIGN}
        if pid is not None
        else prepared_target_was_released(conn, owner)
    )
    return accept_cold_command(conn, owner) if absent else None


def _remaining_budget(conn: Connection, applied_at: datetime) -> float:
    row = conn.execute(
        "SELECT extract(epoch FROM (%s+make_interval(secs=>%s)-clock_timestamp()))",
        (applied_at, BOOT_BUDGET_SEC),
    ).fetchone()
    if row is None:
        raise RuntimeError("lifecycle deadline query returned no row")
    return float(row[0])


def _authorize_attempt(agent_id: int) -> RestartAttempt | bool | None:
    """None means genuinely legacy; False means owned but deferred/observed.

    Authorization commits before OS launch. A crash in that gap consumes an
    attempt, so repeated controller crashes cannot reset the launch limit.
    No transaction waits for a process to exit or for a launch to finish.
    """
    with write_transaction() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
        owner = cur.fetchone()
        if owner is None:
            return False
        if owner["runtime_kind"] is None and owner["runtime_generation"] is None:
            return None
        if owner["machine"] != machine_name() or owner["runtime_kind"] != "process":
            return False
        pointer = owner["lifecycle_command_id"]
        if pointer is None:
            pointer = _accept_if_absent(conn, owner)
            if pointer is None:
                return False
            cur.execute("SELECT * FROM agents_meta WHERE id=%s", (agent_id,))
            owner = cur.fetchone()
            if owner is None:
                raise RuntimeError("cold lifecycle metadata disappeared")
        cur.execute(
            "SELECT * FROM inbound_messages WHERE id=%s AND agent_id=%s FOR UPDATE",
            (pointer, agent_id),
        )
        command = cur.fetchone()
        if command is None:
            raise RuntimeError("owned lifecycle command pointer is invalid")
        if (
            command["target_generation"] != owner["runtime_generation"]
            or command["target_owner"] != owner["runtime_owner"]
            or command["status"] != "claimed"
            or command["applied_at"] is None
            or command["observed_at"] is not None
        ):
            return False
        payload = command["payload"] or {}
        if not isinstance(payload, dict):
            raise TypeError("lifecycle command payload must be an object")
        count = _attempt_count(payload)
        pid = owner["pid"]
        if pid is not None:
            identity = probe_agent_process(pid, agent_id)
            if identity not in {AgentProcessIdentity.GONE, AgentProcessIdentity.FOREIGN}:
                return False
        elif count == 0 and not prepared_target_was_released(conn, owner):
            # NULL is not exit evidence. Only our previous durable authorization
            # explains the prepared state after the original PID was observed gone.
            return False
        if command["kind"] == "terminate":
            if owner["status"] != "terminated":
                return False
            cur.execute(
                "UPDATE inbound_messages SET observed_at=clock_timestamp(),status='done' WHERE id=%s",
                (pointer,),
            )
            cur.execute(
                "UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s "
                "AND lifecycle_command_id=%s",
                (agent_id, pointer),
            )
            return False
        if command["kind"] != "restart" or owner["status"] not in {"restarting", "idling"}:
            return False
        remaining = _remaining_budget(conn, command["applied_at"])
        if remaining <= 0:
            fail_expired_restart(conn, owner, command, payload)
            _log.warning("agent %s command %s failed at its original deadline", agent_id, pointer)
            return False
        if count >= agent_launch._LAUNCH_MAX_RETRIES + 1:
            reason = "launch_attempts_exhausted"
            result = {"outcome": "unobserved", "reason": reason}
            if payload.get("lifecycle_result") != result:
                payload["lifecycle_result"] = result
                cur.execute(
                    "UPDATE inbound_messages SET payload=%s WHERE id=%s", (Jsonb(payload), pointer)
                )
                _log.warning(
                    "agent %s command %s remains unobserved: %s", agent_id, pointer, reason
                )
            return False
        if count and native_proc().has_session(session_name(f"boot-{agent_id}-{pointer}-{count}")):
            return False
        payload["launch_attempts"] = count + 1
        cur.execute("UPDATE inbound_messages SET payload=%s WHERE id=%s", (Jsonb(payload), pointer))
        cur.execute(
            "UPDATE agents_meta SET status='idling',pid=NULL,started_at=NULL,lease_expires_at=NULL "
            "WHERE id=%s",
            (agent_id,),
        )
        return RestartAttempt(
            pointer, count + 1, remaining, owner["config_overlay"], owner["birth_config"]
        )


def recover_lifecycle_command(agent_id: int) -> bool | None:
    """Dispatch one committed launch authorization; never replace a named session."""
    attempt = _authorize_attempt(agent_id)
    if not isinstance(attempt, RestartAttempt):
        return attempt
    agent_launch._launch_agent_process(
        agent_id,
        attempt.config_overlay,
        birth_config=attempt.birth_config,
        confirm=False,
        restart_attempt=(attempt.command_id, attempt.number, attempt.remaining_budget),
    )
    return True
