"""Durable native checkpoint receipts for automatic session handoffs."""

import asyncio
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import HumanMessage

from agent.messages import system_note_message
from shared.agents import GatewayUnavailable
from shared.db_transaction import write_transaction
from shared.impersonation_history import export_handoff, metadata
from shared.message_kwargs import NoteTag
from shared.runtime_incarnation import RuntimeIncarnation


def start_marker(session: dict[str, Any]) -> HumanMessage:
    """Anchor the session's separately retained messages in checkpoint order."""
    note = system_note_message(
        content=f"Impersonation session {session['session_id']} ({session['name']}) "
        f"is preparing. Executor: {session['executor_name']}. Native execution pauses "
        "after this checkpoint is saved; the controller must wait for active status.",
        tag=NoteTag.IMPERSONATION,
        created_at=datetime.now(UTC),
    )
    note.id = f"impersonation-start:{session['agent_id']}:{session['session_id']}"
    note.additional_kwargs["ava_impersonation"] = metadata(session).model_dump()
    return note


async def ensure_start_marker(graph: Any, session: dict[str, Any]) -> None:
    """Repair a crash between accepting the request and saving its timeline anchor."""
    from agent.impersonation import flush_checkpoint

    config = {"configurable": {"thread_id": str(session["agent_id"])}}
    snapshot = await graph.aget_state(config)
    marker = start_marker(session)
    if not any(message.id == marker.id for message in snapshot.values.get("messages", [])):
        await graph.aupdate_state(config, {"messages": [marker]})
    await flush_checkpoint(graph.checkpointer, session["agent_id"])


def _save_document(session: dict[str, Any], incarnation: RuntimeIncarnation) -> tuple[str, str]:
    from psycopg.types.json import Jsonb

    from shared._impersonation_store import OPEN, lock_lease, require_native

    with write_transaction() as conn:
        require_native(conn, incarnation)
        lease = lock_lease(conn, session["id"])
        if lease["status"] in OPEN:
            raise RuntimeError("Cannot hand back an open impersonation session")
        document, path = export_handoff(lease, conn)
        conn.execute(
            "UPDATE agent_impersonations SET handoff_document=%s,handoff_path=%s WHERE id=%s",
            (Jsonb(document), path, lease["id"]),
        )
    summary = lease["summary"]
    if summary is None:
        summary = (
            f"Session ended with status {lease['status']}. "
            f"{lease['rejection_reason'] or ''} No external completion summary was supplied."
        )
    return summary, path


def _receipt(session: dict[str, Any], incarnation: RuntimeIncarnation) -> None:
    from shared._impersonation_store import lock_lease, require_native

    with write_transaction() as conn:
        require_native(conn, incarnation)
        lease = lock_lease(conn, session["id"])
        # These messages have now been delivered in the durable handoff note/file.
        # Their immutable bodies survive independently of processing status.
        conn.execute(
            "UPDATE inbound_messages SET status='done' WHERE agent_id=%s AND status='pending' "
            "AND id IN (SELECT (payload->>'inbound_id')::bigint "
            "FROM agent_impersonation_entries WHERE lease_id=%s AND kind='message' "
            "AND payload->>'direction'='in')",
            (lease["agent_id"], lease["id"]),
        )
        conn.execute(
            "UPDATE agent_impersonations SET handoff_applied_at=clock_timestamp() WHERE id=%s",
            (lease["id"],),
        )


async def deliver_handoff(
    graph: Any, session: dict[str, Any], incarnation: RuntimeIncarnation
) -> None:
    """Save file, append the first resumed input, flush checkpoint, then receipt.

    The native gate remains closed throughout. A crash before the checkpoint
    repeats the same message id; a crash after it only repeats the DB receipt.
    Ordinary queued input cannot be claimed until the receipt releases the gate.
    """
    import httpx

    from agent.impersonation import flush_checkpoint
    from ava._impersonation_events import consume_recorded_events
    from shared.log import logger

    try:
        await asyncio.to_thread(consume_recorded_events, session)
    except (httpx.HTTPError, GatewayUnavailable):
        logger.warning(
            "Impersonation event accounting pending; runner reconciliation will retry",
            agent_id=session["agent_id"],
        )
    summary, path = await asyncio.to_thread(_save_document, session, incarnation)
    config = {"configurable": {"thread_id": str(incarnation.agent_id)}}
    snapshot = await graph.aget_state(config)
    receipt = f"{session['agent_id']}:{session['session_id']}"
    if snapshot.values.get("impersonation_handoff_id") != receipt:
        note = system_note_message(
            content=f"Impersonation session {session['session_id']} ({session['name']}) ended. "
            f"Executor: {session['executor_name']}.\n\nExternal summary:\n{summary}\n\n"
            f"Read the structured handoff: {path}\n"
            "It contains all messages, including ACKed messages, and consumed API/SDK events. "
            "Event accounting may remain pending while upstream delivery catches up. "
            "Incoming messages marked unacknowledged still need your attention. Native execution resumes now.",
            tag=NoteTag.IMPERSONATION,
            created_at=datetime.now(UTC),
        )
        note.id = f"impersonation-handoff:{receipt}"
        await graph.aupdate_state(
            config,
            {
                "messages": [note],
                "impersonation_handoff_id": receipt,
                "halted": False,
                "turn_idle": False,
                "turn_active": True,
            },
        )
    await flush_checkpoint(graph.checkpointer, incarnation.agent_id)
    await asyncio.to_thread(_receipt, session, incarnation)
