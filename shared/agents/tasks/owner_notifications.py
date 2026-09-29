"""Task-owner reassignment notification text and delivery policy."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskOwnerNotification:
    """One task system note and whether its recipient must be resurrected."""

    agent_id: int
    content: str
    resurrect: bool


def owner_change_notifications(
    task_id: int,
    title: str,
    previous_owner: int | None,
    new_owner: int | None,
    *,
    actor: int | None,
    previous_owner_terminated: bool,
    description: str | None = None,
    changes: Sequence[str] | None = None,
) -> list[TaskOwnerNotification]:
    """Build the post-commit notes for a task assignment change.

    A new owner receives a direction and is resurrected when necessary. A
    previous owner receives only an informational note and is never
    resurrected; a terminated previous owner remains asleep. ``actor`` is an
    agent id for SDK updates and ``None`` for a user-originated gateway write.
    """
    notes: list[TaskOwnerNotification] = []
    if new_owner is not None and new_owner != actor:
        actor_suffix = f" (by agent #{actor})" if actor is not None else ""
        content = f'Task #{task_id} "{title}" is now assigned to you{actor_suffix}.'
        if changes:
            content += "\n\n" + "\n".join(f"- {change}" for change in changes)
        if description:
            content += f"\n\n{description}"
        notes.append(TaskOwnerNotification(new_owner, content, resurrect=True))
    if previous_owner is not None and previous_owner != actor and not previous_owner_terminated:
        notes.append(
            TaskOwnerNotification(
                previous_owner,
                f'Task #{task_id} "{title}" you owned is no longer assigned to you.',
                resurrect=False,
            )
        )
    return notes
