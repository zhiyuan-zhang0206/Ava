"""Best-effort live hints published after durable writes commit.

Lifecycle hints name the changed agent without reading or embedding its state.
Consumers reconcile authoritative views, so an older announcement cannot replace
newer state. No database connection is needed on the announcement path.

Redis live hints are separate from durable telemetry facts. Publication failure
must not roll back or fail the already-committed lifecycle write.
"""

from __future__ import annotations

from shared.config import settings
from shared.live_events import (
    AgentSpawned,
    AgentUpdated,
    ImpersonationChanged,
    NoticePosted,
    NoticeResolved,
    PageClosed,
    TaskCreated,
    TaskUpdated,
)
from shared.redis_client import publish_best_effort, publish_best_effort_sync


def publish_agent_spawned_sync(agent_id: int) -> None:
    """Publish a committed creation hint without reading agent state."""
    ev = AgentSpawned(agent_id=agent_id)
    publish_best_effort_sync(
        settings.data_plane.events_channel, ev.model_dump_json(), context="agent_spawned"
    )


def publish_agent_updated_sync(agent_id: int) -> None:
    """Publish a committed lifecycle/display-state hint without a database read."""
    ev = AgentUpdated(agent_id=agent_id)
    publish_best_effort_sync(
        settings.data_plane.events_channel, ev.model_dump_json(), context="agent_updated"
    )


def publish_impersonation_changed_sync(agent_id: int) -> None:
    """Refresh the selected timeline after a committed lease change."""
    ev = ImpersonationChanged(agent_id=agent_id)
    publish_best_effort_sync(
        settings.data_plane.events_channel, ev.model_dump_json(), context="impersonation_changed"
    )


def publish_notice_posted_sync(
    agent_id: int, notice_id: int, priority: str, title: str, task_id: int | None = None
) -> None:
    """Publish NoticePosted from a sync context. The unified Inbox refetches its
    queue from this lightweight header; `task_id` groups it without a refetch."""
    ev = NoticePosted(
        agent_id=agent_id, notice_id=notice_id, priority=priority, title=title, task_id=task_id
    )
    publish_best_effort_sync(
        settings.data_plane.events_channel, ev.model_dump_json(), context="notice_posted"
    )


def publish_notice_resolved_sync(agent_id: int, notice_id: int) -> None:
    """Publish NoticeResolved from a sync context to refresh the Inbox queue.
    This is the same event as the async gateway-side publisher."""
    ev = NoticeResolved(agent_id=agent_id, notice_id=notice_id)
    publish_best_effort_sync(
        settings.data_plane.events_channel, ev.model_dump_json(), context="notice_resolved"
    )


def publish_task_created_sync(agent_id: int, task_id: int) -> None:
    """Publish TaskCreated from a sync context (the ava.tasks.create SDK path).
    No task read — the frontend refetches /api/tasks on receipt, so the event
    only names the task; `agent_id` is the creating agent."""
    ev = TaskCreated(agent_id=agent_id, task_id=task_id)
    publish_best_effort_sync(
        settings.data_plane.events_channel, ev.model_dump_json(), context="task_created"
    )


def publish_task_updated_sync(agent_id: int, task_id: int) -> None:
    """Publish TaskUpdated from a sync context (the ava.tasks.update SDK path).
    See publish_task_created_sync; `agent_id` is the acting agent."""
    ev = TaskUpdated(agent_id=agent_id, task_id=task_id)
    publish_best_effort_sync(
        settings.data_plane.events_channel, ev.model_dump_json(), context="task_updated"
    )


async def publish_agent_updated(agent_id: int) -> None:
    """Publish a committed change hint from an async context."""
    ev = AgentUpdated(agent_id=agent_id)
    await publish_best_effort(
        settings.data_plane.events_channel, ev.model_dump_json(), context="agent_updated"
    )


def publish_page_closed_sync(agent_id: int, name: str) -> None:
    """Publish PageClosed from a sync context (ops lifecycle, launch /
    boot-failure force-terminate paths).

    The `cascade_close_agent_pages` trigger closes only agent-owned show() rows
    when status flips to 'terminated'; daemon-supervised serve() rows remain
    open. Callers capture the former before the status UPDATE and emit one
    PageClosed per name so the frontend removes those entries in real time.
    Best-effort like every announce — a redis outage degrades to "the frontend
    refreshes on its next full fetch"."""
    ev = PageClosed(agent_id=agent_id, name=name)
    publish_best_effort_sync(
        settings.data_plane.events_channel, ev.model_dump_json(), context="page_closed"
    )
