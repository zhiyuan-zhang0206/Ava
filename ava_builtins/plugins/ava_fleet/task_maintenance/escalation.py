"""Accept task escalation from locked current state, before any live hints."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg
from psycopg_pool import ConnectionPool

from base.agents.tasks.status import TaskStatus
from base.config import settings
from base.db import insert_inbound_message_in_transaction
from base.db.transaction import write_transaction


@dataclass(frozen=True)
class EscalationReceipt:
    recipient: int
    task_ids: list[int]
    leg: Literal["user", "delegator"]
    inbound_id: int | None
    content: str


@dataclass(frozen=True)
class _Task:
    id: int
    parent: int | None
    owner: int | None
    title: str
    status: TaskStatus
    reminder_count: int
    escalated_at: datetime | None
    priority: str
    is_root: bool


def _lock_candidates(cur: psycopg.Cursor, threshold: int) -> tuple[list[int], dict[int, _Task]]:
    # Discovery never authorizes an effect. Lock the union in stable id order;
    # SKIP LOCKED avoids reversing a writer's child -> parent lock order.
    # NO KEY UPDATE still fences mutations while allowing a gateway notice
    # creator holding the agent identity lock to reference task_id by FK.
    cur.execute(
        "SELECT id,parent_id FROM agent_tasks WHERE status='in_progress' "
        "AND reminder_count >= %s AND NOT is_root ORDER BY id",
        (threshold,),
    )
    candidates = cur.fetchall()
    ids = sorted({i for task, parent in candidates for i in (task, parent) if i is not None})
    cur.execute(
        "SELECT id,parent_id,owner,title,status,reminder_count,escalated_at,priority,is_root "
        "FROM agent_tasks WHERE id=ANY(%s) ORDER BY id FOR NO KEY UPDATE SKIP LOCKED",
        (ids,),
    )
    locked = {
        row[0]: _Task(
            row[0], row[1], row[2], row[3], TaskStatus(row[4]), row[5], row[6], row[7], row[8]
        )
        for row in cur.fetchall()
    }
    return [row[0] for row in candidates], locked


def _user_notice(cur: psycopg.Cursor, task: _Task) -> EscalationReceipt | None:
    if task.owner is None:
        raise ValueError("human escalation requires a task owner")
    cur.execute(
        "SELECT 1 FROM agent_notices WHERE agent_id=%s AND resolved_at IS NULL LIMIT 1",
        (task.owner,),
    )
    if cur.fetchone() is not None:
        return None
    title = f'Task #{task.id} "{task.title}" stalled after repeated reminders — reassign or cancel'
    content = (
        f"Agent #{task.owner} has not updated this task after repeated reminders, and no "
        "delegating agent owns its parent to catch it. Reassign it to another agent, "
        "cancel it, or reply to remind the owner once more."
    )
    cur.execute(
        "INSERT INTO agent_notices "
        "(agent_id,local_id,task_id,title,content,priority,require_response,blocking,expire_at) "
        "VALUES (%s,COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id=%s),-1)+1, "
        "%s,%s,%s,%s,TRUE,FALSE,now()+make_interval(secs=>%s))",
        (
            task.owner,
            task.owner,
            task.id,
            title,
            content,
            task.priority,
            settings.daemon.notice_ttl_limit_seconds,
        ),
    )
    return EscalationReceipt(task.owner, [task.id], "user", None, content)


def _delegator_note(cur: psycopg.Cursor, owner: int, tasks: list[_Task]) -> EscalationReceipt:
    lines = ["Stalled subtasks — owner(s) unresponsive after repeated reminders:"]
    lines.extend(
        f'- #{task.id} "{task.title}" — owner #{task.owner}, {task.reminder_count} reminders'
        for task in tasks
    )
    content = "\n".join(lines)
    inbound_id, _ = insert_inbound_message_in_transaction(
        cur,
        owner,
        content,
        "system",
        kind="system_note",
        payload={"note_tag": "task"},
    )
    task_ids = [task.id for task in tasks]
    cur.execute("UPDATE agent_tasks SET escalated_at=now() WHERE id=ANY(%s)", (task_ids,))
    return EscalationReceipt(owner, task_ids, "delegator", inbound_id, content)


def _eligible_groups(
    candidates: list[int], locked: dict[int, _Task], threshold: int
) -> tuple[dict[int, list[_Task]], list[_Task]]:
    delegated: dict[int, list[_Task]] = defaultdict(list)
    human: list[_Task] = []
    for task_id in candidates:
        task = locked.get(task_id)
        if (
            task is None
            or task.is_root
            or task.status is not TaskStatus.IN_PROGRESS
            or task.reminder_count < threshold
        ):
            continue
        parent = locked.get(task.parent) if task.parent is not None else None
        if parent is None or task.owner is None:
            continue
        if parent.owner is None:
            human.append(task)
        elif task.escalated_at is None:
            delegated[parent.owner].append(task)
    return delegated, human


def accept_escalations(pool: ConnectionPool, threshold: int) -> list[EscalationReceipt]:
    """Commit each eligible digest/notice from current task and parent rows.

    Parent changes discovered after locking are deferred if the actual parent
    is not locked. Agent identity locks serialize notice creation with gateway
    create's open check and local-id allocation. All live hints happen later.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        candidates, locked = _lock_candidates(cur, threshold)
        delegated, human = _eligible_groups(candidates, locked, threshold)
        # Same physical identity lock as gateway notice create. Ordered locks
        # prevent two overlapping human batches from reversing agent order.
        owners = sorted(set(delegated) | {task.owner for task in human if task.owner is not None})
        cur.execute("SELECT id FROM agents WHERE id=ANY(%s) ORDER BY id FOR UPDATE", (owners,))
        receipts = [
            _delegator_note(cur, owner, tasks) for owner, tasks in sorted(delegated.items())
        ]
        for task in human:
            receipt = _user_notice(cur, task)
            if receipt is not None:
                receipts.append(receipt)
        return receipts
