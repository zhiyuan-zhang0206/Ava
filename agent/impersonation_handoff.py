"""Durable native checkpoint receipts for automatic session endings."""

import asyncio
from typing import Any

from langchain_core.messages import HumanMessage

from agent.messages import system_note_message
from base.agents.impersonation.history import export_handoff, metadata
from base.agents.impersonation.notes import HandoffNotes
from base.agents.messages.kwargs import NoteTag
from base.db import Database, publish_inbound_wake
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation

_IMPERSONATION_EXPLANATION = (
    "Impersonation means a trusted external executor temporarily takes over your "
    "work. You are paused while it acts under your Ava agent identity. It receives "
    "incoming messages, can use your tools, and can send replies to the user as "
    "you; those replies appear to the user as coming from you, an Ava agent. "
    "When the takeover ends, you resume with a summary of its work when supplied "
    "and a path to the session record. Review the handoff and continue any "
    "unfinished requests."
)


def _make_note(content: str, *, notes: HandoffNotes) -> HumanMessage:
    clock = notes.clock_factory()
    created_at = clock.now()
    stamp = f"{clock.format_timestamp(created_at)} " if notes.timestamps_enabled() else ""
    return system_note_message(
        content=stamp + content,
        tag=NoteTag.IMPERSONATION,
        created_at=created_at,
    )


def introduction_note(*, notes: HandoffNotes) -> HumanMessage:
    """Standing native context, introduced only once a takeover is encountered."""
    note = _make_note(_IMPERSONATION_EXPLANATION, notes=notes)
    note.id = "impersonation-introduction"
    return note


def start_update(
    session: dict[str, Any], *, introduced: bool, notes: HandoffNotes
) -> dict[str, Any]:
    """Commit the introduction receipt and the takeover anchor together."""
    introduction = [] if introduced else [introduction_note(notes=notes)]
    return {
        "messages": [*introduction, start_marker(session, notes=notes)],
        "impersonation_introduced": True,
    }


def start_marker(session: dict[str, Any], *, notes: HandoffNotes) -> HumanMessage:
    """Anchor the session's separately retained messages in checkpoint order."""
    note = _make_note(
        f"Impersonation session {session['session_id']} has started. "
        f'An external executor named "{session["name"]}" is taking over your work. '
        "You are paused until it returns control.",
        notes=notes,
    )
    note.id = f"impersonation-start:{session['agent_id']}:{session['session_id']}"
    note.additional_kwargs["ava_impersonation"] = metadata(session).model_dump()
    return note


async def ensure_start_marker(graph: Any, session: dict[str, Any], *, notes: HandoffNotes) -> None:
    """Repair a crash between accepting the request and saving its timeline anchor."""
    from agent.impersonation import flush_checkpoint

    config = {"configurable": {"thread_id": str(session["agent_id"])}}
    snapshot = await graph.aget_state(config)
    update = start_update(
        session, introduced=snapshot.values.get("impersonation_introduced", False), notes=notes
    )
    existing = {message.id for message in snapshot.values.get("messages", [])}
    update["messages"] = [note for note in update["messages"] if note.id not in existing]
    if update["messages"] or not snapshot.values.get("impersonation_introduced", False):
        await graph.aupdate_state(config, update)
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


def _save_document(
    db: Database, session: dict[str, Any], incarnation: RuntimeIncarnation
) -> tuple[str, str]:
    from psycopg.types.json import Jsonb

    from base.agents.impersonation import OPEN, lock_lease, require_native

    with db.write_transaction() as conn:
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


def _receipt(db: Database, session: dict[str, Any], incarnation: RuntimeIncarnation) -> None:
    from base.agents.impersonation import lock_lease, require_native

    with db.write_transaction() as conn:
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
    db: Database,
    bus: EventBus,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    *,
    notes: HandoffNotes,
    reason: str | None = None,
) -> None:
    """Save file, append the first resumed input, flush checkpoint, then receipt.

    The native gate remains closed throughout. A crash before the checkpoint
    repeats the same message id; a crash after it only repeats the DB receipt.
    Ordinary queued input cannot be claimed until the receipt releases the gate.
    The receipt is followed by a best-effort wake: the note's first turn must run
    even when nothing else is queued (claim also resumes on the trailing note, so
    an empty queue is not an idle verdict).

    Event accounting never delays the native checkpoint or its receipt: a log-native
    lease is complete in the database or keeps its documented pending semantics until
    its last open source seals.

    ``reason`` is the death cause of a supervisor-aborted session (task #3998);
    when present, the note names it right after the session-end sentence.
    """
    from agent.impersonation import flush_checkpoint

    # Keep the truthful pending record while the durable native handoff proceeds.
    summary, path = await asyncio.to_thread(_save_document, db, session, incarnation)
    config = {"configurable": {"thread_id": str(incarnation.agent_id)}}
    snapshot = await graph.aget_state(config)
    receipt = f"{session['agent_id']}:{session['session_id']}"
    if snapshot.values.get("impersonation_handoff_id") != receipt:
        stopped = f" This session was stopped early: {reason}." if reason else ""
        note = _make_note(
            f"Impersonation session {session['session_id']} has ended.{stopped} "
            f'You have resumed execution after the takeover by external executor "{session["name"]}".\n\n'
            f"External summary:\n{summary}\n\n"
            f"The session record is available at: {path}\n"
            "Reply to the user in ordinary assistant text in this conversation. "
            "Review the summary and incoming requests in the record. Continue any "
            "requests whose completion is not established by the summary or record. "
            "The activity log may be incomplete; missing entries do not establish "
            "that an action never happened.",
            notes=notes,
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
    await asyncio.to_thread(_receipt, db, session, incarnation)
    # The receipt opens the claim gate; this wake drives the note's first turn when
    # nothing else is queued (and re-drives it if this turn ends before that
    # invocation). Best-effort: the host latches wakes per agent.
    publish_inbound_wake(db, bus, session["agent_id"], "impersonation")
