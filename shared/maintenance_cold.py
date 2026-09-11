"""Recognize completed legacy work only after its local consumers have exited.

This is a read of the existing LangGraph store, not another lifecycle receipt.
An expired lease is insufficient: a persisted END and actual native absence
must agree before cold preparation can retain that idle intent.
"""

from datetime import datetime
from pathlib import Path
from typing import Any, cast

import psutil
import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from shared import exec_request_evidence
from shared.checkpoint_serde import STATIC_CHECKPOINT_MSGPACK_TYPES
from shared.paths import ava_home
from shared.runtime_incarnation import RuntimeIncarnation
from shared.session_backend import get_backend

_CONSUMER_MODULES = frozenset(
    {
        "agent",
        "agent.loop",
        "agent.exec_child",
        "agent.exec_owner_child",
        "agent.exec_domain_owner",
        "services.agent_host.daemon",
    }
)


def require_no_consumers(conn: psycopg.Connection[Any], agent_id: int) -> None:
    """Reject native consumers and unsettled exec requests; retain all PTYs.

    Reuse the native session backend and the host probe's module/home scan.
    Unknown home ownership is not proof of absence. Nothing here signals a
    process or treats a persistent shell, watcher or browser as an exec child.

    Exec request envelopes are judged by incarnation attribution and process
    proof (shared/exec_request_evidence.py). The caller established the retired
    host is absent, so a provably stale envelope is quarantined — preserved
    with a receipt, never deleted — while evidence that is still live or
    unattributable refuses with its file, attribution and disposition commands.
    """
    backend = get_backend()
    if backend.has_session(f"ava-agent-{agent_id}") or backend.list_sessions(
        prefix=f"ava-boot-{agent_id}-"
    ):
        raise RuntimeError(f"legacy native consumer still owns agent {agent_id}")
    row = conn.execute(
        "SELECT runtime_generation,runtime_owner,incarnation_resources FROM agents_meta "
        "WHERE id=%s",
        (agent_id,),
    ).fetchone()
    incumbent = (
        RuntimeIncarnation(agent_id, row[0], row[1])
        if row is not None and row[0] is not None and row[1] is not None
        else None
    )
    report = exec_request_evidence.quarantine_stale(
        agent_id,
        incumbent=incumbent,
        resources=None if row is None else row[2],
        reason="maintenance cold prepare",
    )
    if report.retained:
        raise RuntimeError(
            f"agent {agent_id} still has an unsettled exec request: "
            + "; ".join(entry.describe() for entry in report.retained)
            + ". "
            + exec_request_evidence.disposition_hint(agent_id)
        )
    home = ava_home().resolve()
    for process in psutil.process_iter(["pid", "cmdline"]):
        argv = cast(list[str], process.info["cmdline"] or [])
        if not any(
            argv[i] == "-m" and argv[i + 1] in _CONSUMER_MODULES for i in range(len(argv) - 1)
        ):
            continue
        try:
            raw_home = process.environ().get("AVA_HOME")
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise RuntimeError("cannot identify a native consumer's home") from exc
        if raw_home is None or Path(raw_home).resolve() == home:
            raise RuntimeError(f"native consumer still exists during cold prepare: {process.pid}")


def require_persisted_end(conn: psycopg.Connection[Any], agent_id: int, *, restarting: bool) -> str:
    """Return the latest complete checkpoint ID while the caller holds the row.

    The v4 StateGraph contract represents ready nodes as available branch/start
    channels, and pending tasks, failures and interrupts as pending writes.
    The real compiled-graph tests compare these checks to next/tasks; unknown
    checkpoint versions refuse rather than guessing the dependency's format.
    """
    meta = conn.execute(
        "SELECT lifecycle_command_id,runtime_generation,runtime_owner FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone()
    if meta is None or meta[0] is not None:
        raise RuntimeError(f"cold agent {agent_id} still owns a lifecycle command")
    failure = conn.execute(
        "SELECT 1 FROM inbound_messages WHERE agent_id=%s AND kind IN ('restart','terminate') "
        "AND payload->'lifecycle_result'->>'outcome'='failed' "
        "AND target_generation=%s AND target_owner=%s LIMIT 1",
        (agent_id, meta[1], meta[2]),
    ).fetchone()
    if failure is not None:
        raise RuntimeError(f"cold agent {agent_id} has a failed lifecycle command")
    after: datetime | None = None
    if restarting:
        command = conn.execute(
            "SELECT claimed_at FROM inbound_messages WHERE agent_id=%s AND kind='restart' "
            "AND status='done' AND claimed_at IS NOT NULL AND target_owner IS NULL "
            "AND target_generation IS NULL AND applied_at IS NULL AND observed_at IS NULL "
            "AND NOT COALESCE(payload ? 'lifecycle_result',false) "
            "AND id=(SELECT max(id) FROM inbound_messages WHERE agent_id=%s "
            "AND kind IN ('restart','terminate'))",
            (agent_id, agent_id),
        ).fetchone()
        if command is None:
            raise RuntimeError(f"agent {agent_id} has no completed legacy restart")
        after = command[0]
    serde = JsonPlusSerializer(allowed_msgpack_modules=STATIC_CHECKPOINT_MSGPACK_TYPES)
    saved = PostgresSaver(conn, serde=serde).get_tuple(
        {"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}}
    )
    if saved is None:
        raise RuntimeError("maintenance requires the live original native owner or a cold END")
    checkpoint = saved.checkpoint
    values = checkpoint["channel_values"]
    if (
        checkpoint["v"] != 4
        or saved.pending_writes
        or any(key.startswith(("__", "branch:")) for key in values)
        or (restarting and values.get("exit_requested") is not True)
        or values.get("restart_requested") is not False
        or values.get("halted") is not True
        or (after is not None and datetime.fromisoformat(checkpoint["ts"]) <= after)
    ):
        raise RuntimeError(f"agent {agent_id} has no complete persisted cold END")
    return checkpoint["id"]


def normalize_retired_intent(
    conn: psycopg.Connection[Any], agent_id: int, *, restarting: bool
) -> None:
    """Only restore the parked status; preserve the lease, identities and history."""
    require_no_consumers(conn, agent_id)
    checkpoint_id = require_persisted_end(conn, agent_id, restarting=restarting)
    # A writer outside native admission must not replace the evidence between
    # the read and normalization. The original row lock still binds all owner
    # fields; this final statement also rechecks the latest checkpoint identity.
    if not restarting:
        latest = conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id=%s "
            "AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1",
            (str(agent_id),),
        ).fetchone()
        if latest != (checkpoint_id,):
            raise RuntimeError(f"cold checkpoint changed while preparing agent {agent_id}")
        return
    changed = conn.execute(
        "UPDATE agents_meta SET status='idling' WHERE id=%s "
        "AND status=%s AND lifecycle_command_id IS NULL "
        "AND (SELECT checkpoint_id FROM checkpoints WHERE thread_id=%s "
        "AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1)=%s",
        (agent_id, "restarting", str(agent_id), checkpoint_id),
    )
    if changed.rowcount != 1:
        raise RuntimeError(f"cold checkpoint changed while preparing agent {agent_id}")
