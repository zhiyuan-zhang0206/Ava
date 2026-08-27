---
type: doc
title: Task Notification — Task Notification Mechanism
description: Automatic notification on owner change — what messages new and old owners receive, new owner always notified (auto-revive if terminated), old owner is the only leg that may be skipped due to terminated status, integration with send_message
tags:
- fleet
- tasks
- notification
- messaging
---

# Task Notification — Task Notification Mechanism

## When it triggers

Two places trigger the same `_notify_owner_change`:
- In `update`, when **owner changes** and the new and old owners are different — merely changing `status` or `results` does not generate a notification; only when a task is transferred from one person to another.
- In `create`, when an **owner other than the creator** is specified (including `create_and_assign`) — the assigned agent is notified as soon as the task is created.

## Notification Content

The system sends one message to each direction:

| Direction | Message Template | Meaning |
|------|----------|------|
| **New owner** | `Task #%d "%s" is now assigned to you (by agent #%d).` | Notifies that a new task has arrived |
| **Old owner** | `Task #%d "%s" you owned is no longer assigned to you.` | Notifies that the task has left their hands |

When triggered from `create` (with `old_owner=None`), the new owner message also appends the task's `description` (separated by `\n\n`), so the receiving agent knows what to do without a separate `get` call.

## Skip Conditions

The skip rules for the two legs are asymmetric — the new owner is always notified, only the old owner leg may be skipped due to termination status:

**New owner leg** (whether to send "task assigned to you"):

| Condition | Reason |
|------|------|
| `new_owner is None` | Defensive: only the root task has no owner, a normal transfer path won't hit this |
| `new_owner == actor` (caller themselves) | No need to tell themselves they've taken over |

A new owner that is already `terminated` is still sent — the system-note delivery (`send_system_note`, `resurrect=True`) revives a terminated target automatically. This prevents tasks from becoming stranded when assigning to a terminated agent (the assignment notification and the task body are delivered together to the revived owner).

**Old owner leg** (whether to send "task left you"):

| Condition | Reason |
|------|------|
| `old_owner is None` | When triggered from the create assignment path, there is no old owner — no old owner to notify |
| `old_owner == actor` (caller themselves) | No need to tell themselves "you shouldn't do this" (they transferred their own task) |
| Old owner is already `terminated` / does not exist in `agents_meta` | **The only preserved terminated skip** — waking up a terminated previous owner just to say the task is gone is wasteful, and they didn't ask to be revived |

## Delivery: system note, not peer chat (user ruling 2026-08-27)

Task notifications are **system notes** (`ava.agents.send_system_note`, NoteTag
`task`): they render in the receiving agent's timeline as a system_marker —
no Agent prefix, no peer timestamp — consistent with other system notes. They
are delivered through the inbound queue as kind=`system_note` rows and claimed
into `ava_msg_type='system_note'` messages; the timeline dispatches on the
NoteTag, so the rendering change needs no frontend special-casing.

## Implementation Details

```python
def _notify_owner_change(task_id, title, old_owner, new_owner, actor, description=None):
    # New owner leg: always notify (terminated agents are auto-revived —
    # an assignment is a delegator direction, so the system-note delivery resurrects)
    if new_owner is not None and new_owner != actor:
        new_msg = f'Task #{task_id} "{title}" is now assigned to you (by agent #{actor}).'
        if description:                   # Only passed from the create assignment path
            new_msg += f"\n\n{description}"
        ava.agents.send_system_note(new_owner, new_msg, resurrect=True)
    # Old owner leg: the only leg that may be skipped due to terminated status;
    # resurrect=False — a notification must never revive an owner
    if old_owner is not None and old_owner != actor and not _is_terminated(old_owner):
        ava.agents.send_system_note(
            old_owner, f'Task #{task_id} "{title}" you owned is no longer assigned to you.',
            resurrect=False,
        )


def _is_terminated(agent_id):               # Only gates the old owner leg
    with ava.DB.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        meta = cur.fetchone()
    return meta is None or meta[0] == "terminated"
```

## Execution Timing

Notifications are executed **after the transaction commits**, not inside the database transaction:

```python
with ava.DB.transaction(), ava.DB.cursor() as cur:
    # ... SELECT FOR UPDATE, UPDATE, event_log ...

# ← Transaction commits here
if owner_changing and old_owner != new_owner:
    _notify_owner_change(...)  # Executed outside the transaction
```

This design has two reasons:
1. `send_message` may auto-revive a terminated agent — this is a side-effect operation that should not be performed while holding locks.
2. If notification fails, the task state is already persisted — we don't want to roll back business data due to a notification failure.

## Relationship with Other Notifications

- Task owner change notifications are **system notes** (sent via `agents.send_system_note`, NoteTag `task`), not peer chat messages.
- Task completion results should be presented to the **user** via [[../notify.ava.okf.md|`ava.ui.notify`]].
- These two types of notifications are not substitutes for each other — the former lets the receiving agent know about a new task, the latter lets the human supervisor see the result.
- A third, different line: `create`/`update` also publishes `task_created`/`task_updated` events on each write (SSE, visible to `GLOBAL_ROLES`) — these are not directed messages but cause all open task boards to invalidate and re-fetch; overdue task escalation (if parent has an owner → send a chat to the parent owner; if parent has no owner → insert a require_response notice on the stuck owner) is covered in [[../task_maintenance/task-maintenance.ava.okf.md|Task-Maintenance]] and is not part of this node.
