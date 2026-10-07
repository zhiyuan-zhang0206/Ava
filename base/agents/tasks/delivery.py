"""Task notifications committed directly to the durable inbound queue."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import LiteralString

import psycopg

from base import telemetry
from base.agents.messages.inbound import InboundKind
from base.agents.tasks.owner_notifications import TaskOwnerNotification
from base.db import insert_inbound_message_in_transaction

# Watchdog eligibility; the resurrection transaction locks the same task. Alias m is
# the inbound row; integer ids compare as text to reject malformed JSON safely.
TASK_ASSIGNMENT_CURRENT: LiteralString = (
    "(m.kind='system_note' AND m.payload->'task_notification'='true'::jsonb "
    "AND m.payload->'delivery_resurrect'='true'::jsonb "
    "AND EXISTS(SELECT 1 FROM agent_tasks task "
    "WHERE task.id::text=m.payload->>'task_id' AND task.owner=m.agent_id))"
)


@dataclass(frozen=True)
class TaskNoteReceipt:
    """One committed notification intent and its optional post-commit audit emit."""

    agent_id: int
    inbound_id: int
    content: str
    source: str
    resurrect: bool
    event: telemetry.Event | None


def enqueue_task_notifications(
    cur: psycopg.Cursor,
    task_id: int,
    notes: Sequence[TaskOwnerNotification],
    source: str,
) -> list[TaskNoteReceipt]:
    """Insert immutable note/policy snapshots without committing or waking."""
    receipts: list[TaskNoteReceipt] = []
    for note in notes:
        payload: dict[str, object] = {
            "note_tag": "task",
            "task_notification": True,
            "delivery_resurrect": note.resurrect,
        }
        if note.link_task:
            payload["task_id"] = task_id
        inbound_id, event = insert_inbound_message_in_transaction(
            cur,
            note.agent_id,
            note.content,
            source,
            kind=InboundKind.SYSTEM_NOTE.value,
            payload=payload,
        )
        receipts.append(
            TaskNoteReceipt(note.agent_id, inbound_id, note.content, source, note.resurrect, event)
        )
    return receipts


def supersede_task_assignments(cur: psycopg.Cursor, task_id: int) -> None:
    """Settle unclaimed old directions before a new assignment is inserted.

    The producer holds the task row lock. Closing all earlier assignment
    intents also fences A -> B -> A: the first A direction never becomes new
    work when A regains ownership. Informational notes remain deliverable.
    """
    cur.execute(
        "UPDATE inbound_messages SET status='done', claimed_at=now(), "
        "payload=payload||jsonb_build_object('delivery_result', "
        "jsonb_build_object('outcome','superseded','reason','task_reassigned')) "
        "WHERE kind='system_note' AND status='pending' "
        "AND payload->'task_notification'='true'::jsonb "
        "AND payload->'delivery_resurrect'='true'::jsonb "
        "AND payload->>'task_id'=%s",
        (str(task_id),),
    )


def emit_task_note_events(receipts: Sequence[TaskNoteReceipt]) -> None:
    """Emit prepared telemetry only after the producer's transaction commits."""
    for receipt in receipts:
        if receipt.event is not None:
            telemetry.emit_prepared(receipt.event)


def lock_task_note_owner(cur: psycopg.Cursor, agent_id: int, inbound_id: int) -> bool:
    """Fence task-linked automatic wakes at their final home-side transaction.

    The caller locks the task before agents_meta. This keeps a concurrent
    reassignment from changing ownership between this check and wake commit.
    A missing task is stale work; non-task triggers use their normal fences.
    """
    cur.execute(
        "SELECT payload->>'task_id' FROM inbound_messages "
        "WHERE id=%s AND agent_id=%s AND kind='system_note' AND payload ? 'task_id'",
        (inbound_id, agent_id),
    )
    row = cur.fetchone()
    if row is None:
        return True
    cur.execute("SELECT owner FROM agent_tasks WHERE id::text=%s FOR UPDATE", (row[0],))
    task = cur.fetchone()
    return task is not None and task[0] == agent_id
