"""SSE live projection — lifecycle announce publishers.

AgentSpawned / AgentUpdated / NoticePosted / TaskCreated etc. are **live
projections**: after a durable write commits, these helpers broadcast the new
snapshot over the Redis `ava:events` channel for the live UI. They are never
persisted and are distinct from the unified events table facts
(`shared/telemetry.py` — `Event` LogRecord facts that land in the `events`
table, the durable source of truth). The announce is a render hint for the
frontend, not a fact.

Publish helpers for AgentSpawned / AgentUpdated SSE events.

One call site per agent lifecycle write: after the agents_meta UPDATE
commits, call one of these to broadcast the new snapshot. The frontend
sidebar subscribes to the events channel and upserts its agents list by
id.

Two transports because writers live in both sync (FastAPI threadpool
handlers, agent process startup) and async (agent graph nodes) contexts.
Both helpers do the same work: SELECT the canonical snapshot, build the
event, publish JSON to the Redis events channel.

Both take the caller's connection (sync) or pool (async) instead of
opening a fresh connection per event — publishing happens on every agent
status flip, and a per-event `connect()` multiplied across a fleet of
agents is exactly what exhausted Postgres `max_connections` and crashed
agents mid-turn.
"""

from __future__ import annotations

import psycopg
from psycopg_pool import AsyncConnectionPool

from shared.agent_snapshot import select_one, select_one_async
from shared.config import settings
from shared.live_events import (
    AgentSpawned,
    AgentUpdated,
    NoticePosted,
    NoticeResolved,
    PageClosed,
    TaskCreated,
    TaskUpdated,
)
from shared.redis_client import publish_best_effort, publish_best_effort_sync

# Every helper below is a post-commit announce: the caller has already committed
# its durable write, and these only broadcast the new snapshot for the live UI.
# The publish is therefore routed through the best-effort primitives, which never
# raise — a redis outage must degrade to "the frontend refreshes on its next full
# fetch", never propagate back and crash the caller (a raise at the tail of
# `mark_agent_status` / `claim_agent_row` used to kill the agent process).


def publish_agent_spawned_sync(conn: psycopg.Connection, agent_id: int) -> None:
    """Publish AgentSpawned from a sync context (gateway threadpool route,
    agent process startup), reading the snapshot on the caller's connection.
    Skips silently when the row is missing — the caller may have committed
    and rolled back; do not let publish noise raise into the lifecycle path."""
    snap = select_one(conn, agent_id)
    if snap is None:
        return
    ev = AgentSpawned(agent_id=agent_id, snapshot=snap)
    publish_best_effort_sync(
        settings.data_plane.events_channel, ev.model_dump_json(), context="agent_spawned"
    )


def publish_agent_updated_sync(conn: psycopg.Connection, agent_id: int) -> None:
    """Publish AgentUpdated from a sync context. See publish_agent_spawned_sync."""
    snap = select_one(conn, agent_id)
    if snap is None:
        return
    ev = AgentUpdated(agent_id=agent_id, snapshot=snap)
    publish_best_effort_sync(
        settings.data_plane.events_channel, ev.model_dump_json(), context="agent_updated"
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


async def publish_agent_updated(pool: AsyncConnectionPool, agent_id: int) -> None:
    """Publish AgentUpdated from an async context (agent graph nodes),
    borrowing the snapshot read from the caller's pool. Skips silently
    when the row is missing."""
    async with pool.connection() as conn:
        snap = await select_one_async(conn, agent_id)
    if snap is None:
        return
    ev = AgentUpdated(agent_id=agent_id, snapshot=snap)
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
