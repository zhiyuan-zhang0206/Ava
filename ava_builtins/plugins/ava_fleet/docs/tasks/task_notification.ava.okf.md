---
type: doc
title: Task Notification — Task Notification Mechanism
description: Automatic notification on owner change — what messages new and old owners receive, new owner always notified (auto-revive if terminated), old owner is the only leg that may be skipped due to terminated status, integration with task-tagged system notes
tags:
- fleet
- tasks
- notification
- messaging
---

# Task Notification — Task Notification Mechanism

## When it triggers

Two producers use the same owner-change notification policy:
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

A new owner that is already `terminated` is still sent — the queued assignment records `delivery_resurrect=true`; delivery recovery revives a terminated target automatically. This prevents tasks from becoming stranded when assigning to a terminated agent (the assignment notification and the task body are delivered together to the revived owner).

**Old owner leg** (whether to send "task left you"):

| Condition | Reason |
|------|------|
| `old_owner is None` | When triggered from the create assignment path, there is no old owner — no old owner to notify |
| `old_owner == actor` (caller themselves) | No need to tell themselves "you shouldn't do this" (they transferred their own task) |
| Old owner is already `terminated` / does not exist in `agents_meta` | **The only preserved terminated skip** — waking up a terminated previous owner just to say the task is gone is wasteful, and they didn't ask to be revived |

## Delivery: system note, not peer chat (user ruling 2026-08-27)

Task notifications are **system notes** (NoteTag
`task`): they render in the receiving agent's timeline as a system_marker —
no Agent prefix, no peer timestamp — consistent with other system notes. They
are delivered through the inbound queue as kind=`system_note` rows and claimed
into `ava_msg_type='system_note'` messages; the timeline dispatches on the
NoteTag, so the rendering change needs no frontend special-casing.

## Durable acceptance and recovery

SDK task create/update and gateway task PATCH insert the notification directly
into `inbound_messages` in the same Postgres transaction as the task mutation.
The existing inbound queue is the server outbox: there is no second outbox table
or client-side delivery queue. A committed task has its immutable notification
content, recipient and `delivery_resurrect` policy committed with it. Failed
notification acceptance rolls the mutation back; failed post-commit telemetry,
Redis hints or resurrection do not erase that accepted intent.

The normal watchdog pending scan repairs live-owner wakes. Its terminated-owner
recovery also selects internal task assignments marked `task_notification=true`
and `delivery_resurrect=true`, linked to a task currently owned by that recipient.
Informational previous-owner notes, update notes and reminders never resurrect.

Reassignment closes earlier pending assignment directions in the task transaction,
with `delivery_result={outcome: superseded, reason: task_reassigned}`. This also
fences A -> B -> A: the first A assignment cannot become new work again. The home
resurrection transaction locks the linked task before the agent and holds that
ownership fence through commit, refusing an in-flight stale notification.
Already claimed notes retain normal inbound processing semantics; this queue
cannot revoke an instruction an agent has already consumed.

Existing force-termination, suppression, recovery-breaker, attempt cooldown and
age limits still apply. Once the terminated-owner age deadline expires, the
row is retained as `done` with
`delivery_result={outcome: failed, reason: resurrection_deadline_expired}` so an
operator can inspect the accepted instruction and its final failure.

The task write itself remains keyless and is not automatically retried after an
ambiguous response. This closes the task-effect/notification crash gap; it does
not make repeated task create calls the same operation.

## Relationship with Other Notifications

- Task owner change notifications are **system notes** (accepted into the inbound queue, NoteTag `task`), not peer chat messages.
- Task completion results should be presented to the **user** via [[../notify.ava.okf.md|`ava.ui.notify`]].
- These two types of notifications are not substitutes for each other — the former lets the receiving agent know about a new task, the latter lets the human supervisor see the result.
- A third, different line: `create`/`update` also publishes `task_created`/`task_updated` events on each write (SSE, visible to `GLOBAL_ROLES`) — these are not directed messages but cause all open task boards to invalidate and re-fetch; overdue task escalation (if parent has an owner → send a chat to the parent owner; if parent has no owner → insert a require_response notice on the stuck owner) is covered in [[../../task_maintenance/docs/task-maintenance.ava.okf.md|Task-Maintenance]] and is not part of this node.
