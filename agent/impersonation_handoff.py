"""Durable native checkpoint receipts for automatic session endings."""

import asyncio
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import HumanMessage

from agent.messages import system_note_message
from shared.agents import GatewayUnavailable
from shared.db import publish_inbound_wake
from shared.db_transaction import write_transaction
from shared.impersonation_history import export_handoff, metadata
from shared.message_kwargs import NoteTag
from shared.runtime_incarnation import RuntimeIncarnation


def start_marker(session: dict[str, Any]) -> HumanMessage:
    """Anchor the session's separately retained messages in checkpoint order."""
    note = system_note_message(
        content=f"Impersonation session {session['session_id']} has started. "
        f'The takeover identifies itself as "{session["name"]}". '
        "Your execution pauses at the checkpoint saved with this note; "
        "the session's end delivers a closing note that resumes your execution.",
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


def resume_note_pending(state: Any) -> bool:
    """Whether the newest message is an end-of-session note awaiting its first turn.

    `deliver_handoff` appends the note as the first resumed input. It is a system
    note, so a window that never carried a real exchange still reads as "no
    conversation" and the claim would idle out with the note unprocessed. Until
    the agent takes its first turn after delivery (which appends newer messages),
    the trailing note itself marks the pending resume.
    """
    receipt = state.impersonation_handoff_id
    messages = state.messages
    if receipt is None or not messages:
        return False
    return getattr(messages[-1], "id", None) == f"impersonation-handoff:{receipt}"


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
        # These messages have now been delivered in the durable end-of-session note
        # and record file.
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
    graph: Any,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    *,
    reason: str | None = None,
) -> None:
    """Save file, append the first resumed input, flush checkpoint, then receipt.

    The native gate remains closed throughout. A crash before the checkpoint
    repeats the same message id; a crash after it only repeats the DB receipt.
    Ordinary queued input cannot be claimed until the receipt releases the gate.
    The receipt is followed by a best-effort wake: the note's first turn must run
    even when nothing else is queued (claim also resumes on the trailing note, so
    an empty queue is not an idle verdict).

    ``reason`` is the death cause of a supervisor-aborted session (task #3998);
    when present, the note names it right after the session-end sentence.
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
        stopped = f" This session was stopped early: {reason}." if reason else ""
        note = system_note_message(
            content=f"Impersonation session {session['session_id']} has ended.{stopped} "
            f'The takeover identified itself as "{session["name"]}".\n\nExternal summary:\n{summary}\n\n'
            f"If you need more detail than the summary, the complete structured "
            f"record of this session is available at: {path}\n"
            "It retains all messages, including ACKed messages, and the consumed API/SDK events. "
            "Event accounting may remain pending while upstream delivery catches up. "
            "Incoming messages marked unacknowledged still need your attention. "
            "Your execution resumes with this note.",
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
    # The receipt opens the claim gate; this wake drives the note's first turn when
    # nothing else is queued (and re-drives it if this turn ends before that
    # invocation). Best-effort: the host latches wakes per agent.
    publish_inbound_wake(session["agent_id"], "impersonation")
