"""Best-effort live hints published after durable writes commit.

Lifecycle hints name the changed agent without reading or embedding its state.
Consumers reconcile authoritative views, so an older announcement cannot replace
newer state. No database connection is needed on the announcement path.

Redis live hints are separate from durable telemetry facts. Publication failure
must not roll back or fail the already-committed lifecycle write.
"""

from __future__ import annotations

from base.events.live.bus import EventBus
from base.events.live.projection import (
    AgentSpawned,
    AgentUpdated,
    ImpersonationChanged,
    TaskCreated,
    TaskUpdated,
)


def publish_agent_spawned_sync(bus: EventBus, agent_id: int) -> None:
    """Publish a committed creation hint without reading agent state."""
    ev = AgentSpawned(agent_id=agent_id)
    bus.publish_best_effort_sync(ev.model_dump_json(), context="agent_spawned")


def publish_agent_updated_sync(bus: EventBus, agent_id: int) -> None:
    """Publish a committed lifecycle/display-state hint without a database read."""
    ev = AgentUpdated(agent_id=agent_id)
    bus.publish_best_effort_sync(ev.model_dump_json(), context="agent_updated")


def publish_impersonation_changed_sync(bus: EventBus, agent_id: int) -> None:
    """Refresh the selected timeline after a committed lease change."""
    ev = ImpersonationChanged(agent_id=agent_id)
    bus.publish_best_effort_sync(ev.model_dump_json(), context="impersonation_changed")


def publish_task_created_sync(bus: EventBus, agent_id: int, task_id: int) -> None:
    """Publish TaskCreated from a sync context (the ava.tasks.create SDK path).
    No task read — the frontend refetches /api/tasks on receipt, so the event
    only names the task; `agent_id` is the creating agent."""
    ev = TaskCreated(agent_id=agent_id, task_id=task_id)
    bus.publish_best_effort_sync(ev.model_dump_json(), context="task_created")


def publish_task_updated_sync(bus: EventBus, agent_id: int, task_id: int) -> None:
    """Publish TaskUpdated from a sync context (the ava.tasks.update SDK path).
    See publish_task_created_sync; `agent_id` is the acting agent."""
    ev = TaskUpdated(agent_id=agent_id, task_id=task_id)
    bus.publish_best_effort_sync(ev.model_dump_json(), context="task_updated")


async def publish_agent_updated(bus: EventBus, agent_id: int) -> None:
    """Publish a committed change hint from an async context."""
    ev = AgentUpdated(agent_id=agent_id)
    await bus.publish_best_effort(ev.model_dump_json(), context="agent_updated")
