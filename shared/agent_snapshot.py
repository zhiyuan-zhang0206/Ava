"""AgentSnapshot — full lifecycle state of one agent. Single source of truth
for three transports: HTTP (GET /api/agents returns it as AgentRow), SSE
(AgentSpawned / AgentUpdated events carry it), and frontend rendering.

`select_one` / `select_all` helpers wrap the canonical JOIN query so all
producers (gateway list endpoints + lifecycle publish helpers) compute
`last_active_at` identically.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, LiteralString, cast

import psycopg
from pydantic import BaseModel, Field

from shared.agents import AgentStatus
from shared.config import settings
from shared.lm.factory import model_supports_vision
from shared.priority import Priority

# Canonical columns + JOIN. last_active_at is the agent's REAL-activity clock
# (agents_meta.last_active_at, written by the agent process on every completed
# LLM turn — agent/graph/_llm.py) with a spawned_at fallback for brand-new
# rows; last_inbound_at is MAX(inbound_messages.created_at) across all kinds
# (chat / lifecycle / compact all count as "when did anyone last talk to it"),
# with the same fallback. The two diverge exactly for an agent grinding on a
# long single turn — the case where "is it alive" is the real question
# (issue #183). agents JOIN agents_meta on id; t.label is the user-editable
# thread name.
# notices_awaiting_response is a correlated scalar subquery (a json array —
# json has no equality operator, so it never joins) over the agent's open
# require_response notices (migration 0053) — the bounded "needs response"
# worklist. unread_notice_count is a scalar subquery counting the agent's open
# FYI notices (require_response false): the FYI content stays off the snapshot
# (its own paginated feed, since a backlog could be large), only the badge count
# rides here. Both subqueries and the lateral lookups are per-agent (indexed),
# so the whole snapshot is O(agents), not O(agents x inbound rows).
_FULL_COLS = (
    "a.id, a.spawner, a.fork_source_agent_id, "
    "a.fork_source_checkpoint_id, a.status, a.pid, "
    "a.spawned_at, a.started_at, "
    "COALESCE(a.last_active_at, a.started_at, a.spawned_at) AS last_active_at, "
    "COALESCE(im.last_inbound_at, a.started_at, a.spawned_at) AS last_inbound_at, "
    "t.label, a.machine, "
    "a.heartbeat_paused_until, "
    "a.liveness_state, a.last_probe_at, "
    "COALESCE("
    "(SELECT json_agg(json_build_object("
    "'id', n.id, 'title', n.title, 'content', n.content, 'priority', n.priority, "
    "'blocking', n.blocking, 'created_at', n.created_at, 'task_id', n.task_id) ORDER BY n.created_at) "
    "FROM agent_notices n "
    "WHERE n.agent_id = a.id AND n.require_response AND n.resolved_at IS NULL), "
    "'[]'::json) AS notices_awaiting_response, "
    "(SELECT count(*) FROM agent_notices n "
    "WHERE n.agent_id = a.id AND NOT n.require_response AND n.resolved_at IS NULL "
    # Expired FYI notices (older than NOTICE_FYI_TTL_DAYS, audit C1) do not
    # count toward the unread badge — the feed and the IM bridge drop them the
    # same way. The TTL is inlined as a plain SQL literal (the snapshot query
    # takes no params and stays a pure string constant, per the repo's
    # no-interpolation preference); keep the '30' in sync with
    # shared.db.NOTICE_FYI_TTL_DAYS — the C1 tests lock the coupling.
    "AND n.created_at > now() - interval '30 days') AS unread_notice_count"
    ", a.config_overlay"
)
# The list summary deliberately has its own SELECT, rather than serializing a
# full snapshot and dropping keys afterwards. `effective_model` is the one
# scalar needed to preserve `supports_vision`; the raw JSONB config overlay
# never leaves Postgres on this path.
_SUMMARY_COLS = (
    "a.id, a.spawner, a.fork_source_agent_id, a.status, a.pid, "
    "a.spawned_at, a.started_at, "
    "COALESCE(a.last_active_at, a.started_at, a.spawned_at) AS last_active_at, "
    "COALESCE(im.last_inbound_at, a.started_at, a.spawned_at) AS last_inbound_at, "
    "t.label, a.machine, a.heartbeat_paused_until, a.liveness_state, "
    "COALESCE("
    "(SELECT json_agg(json_build_object("
    "'id', n.id, 'title', n.title, 'content', n.content, 'priority', n.priority, "
    "'blocking', n.blocking, 'created_at', n.created_at, 'task_id', n.task_id) ORDER BY n.created_at) "
    "FROM agent_notices n "
    "WHERE n.agent_id = a.id AND n.require_response AND n.resolved_at IS NULL), "
    "'[]'::json) AS notices_awaiting_response, "
    "(SELECT count(*) FROM agent_notices n "
    "WHERE n.agent_id = a.id AND NOT n.require_response AND n.resolved_at IS NULL "
    "AND n.created_at > now() - interval '30 days') AS unread_notice_count, "
    "a.config_overlay ->> 'llm_model' AS effective_model"
)
_FROM = (
    "FROM agents_meta a "
    "JOIN agents t ON t.id = a.id "
    # last_active_at via LATERAL MAX, not a full LEFT JOIN: the plain join
    # multiplied every agent by its inbound row count (50K intermediate rows at
    # 2.7K agents) and forced a GROUP BY on the whole product (audit P1-1). One
    # lateral row per agent — index-only MAX over
    # inbound_messages_agent_id_created_at_idx — keeps select_all O(agents).
    "LEFT JOIN LATERAL ("
    "SELECT MAX(created_at) AS last_inbound_at FROM inbound_messages im "
    "WHERE im.agent_id = a.id"
    ") im ON true "
)
# Every join above yields at most one row per agent (agents PK, LATERAL MAX),
# and the notices are scalar subqueries — no GROUP BY needed.
_GROUP = ""

AgentListScope = Literal["all", "live", "terminated"]
AgentListFields = Literal["full", "summary", "compact"]


# Scope is part of the query text, not a post-fetch Python filter.  The three
# statements are fixed literals selected from the validated vocabulary, so a
# live roster never evaluates the per-agent LATERAL / notice subqueries for
# historical terminated rows.
def _select_all_sql(columns: str) -> dict[AgentListScope, LiteralString]:
    """Build the three fixed list statements for one trusted column projection."""
    return {
        "all": cast(LiteralString, f"SELECT {columns} {_FROM} {_GROUP} ORDER BY a.id"),
        "live": cast(
            LiteralString,
            f"SELECT {columns} {_FROM} WHERE a.status <> 'terminated' {_GROUP} ORDER BY a.id",
        ),
        "terminated": cast(
            LiteralString,
            f"SELECT {columns} {_FROM} WHERE a.status = 'terminated' {_GROUP} ORDER BY a.id",
        ),
    }


_SELECT_ALL_SQL: dict[AgentListFields, dict[AgentListScope, LiteralString]] = {
    "full": _select_all_sql(_FULL_COLS),
    "summary": _select_all_sql(_SUMMARY_COLS),
    # This deliberately does not reuse `_FROM`: the CLI needs only three
    # columns, so its query must avoid the roster's LATERAL and notice lookups.
    "compact": {
        "all": (
            "SELECT a.id, a.status, t.label FROM agents_meta a JOIN agents t ON t.id = a.id "
            "ORDER BY a.id"
        ),
        "live": (
            "SELECT a.id, a.status, t.label FROM agents_meta a JOIN agents t ON t.id = a.id "
            "WHERE a.status <> 'terminated' ORDER BY a.id"
        ),
        "terminated": (
            "SELECT a.id, a.status, t.label FROM agents_meta a JOIN agents t ON t.id = a.id "
            "WHERE a.status = 'terminated' ORDER BY a.id"
        ),
    },
}


class OpenNotice(BaseModel):
    """One open notice from ava.ui.notify() (migration 0053).

    The agent snapshot's `notices_awaiting_response` carries only the require_response
    subset (the "needs response" worklist); this model also serves the per-agent
    inspector panel where the single open notice (since #152) may be either kind.

    `title` is the one-line headline; `content` is the optional detail
    body (possibly long). `priority` is P0..P3 (the stakes axis) and `blocking`
    says whether the agent is stalled waiting on the answer (the urgency axis) —
    the two triage axes (color + sort). `require_response` is True when the agent
    needs a user decision, False for a one-way FYI; defaults to True for backward
    compat with the snapshot's notices_awaiting_response subquery (which only
    selects require_response rows).
    """

    id: int
    title: str
    content: str | None
    priority: Priority
    require_response: bool = Field(default=True)
    blocking: bool
    created_at: datetime
    # The task this notice belongs to, or None — the human queue groups notices
    # by it (falling back to the owner agent's task when absent).
    task_id: int | None = None


class AgentListSummary(BaseModel):
    """Roster fields used by list consumers; full details stay on demand."""

    agent_id: int
    spawner: str
    fork_source_agent_id: int | None
    status: AgentStatus
    pid: int | None
    spawned_at: datetime
    started_at: datetime | None
    last_active_at: datetime
    last_inbound_at: datetime
    label: str | None
    machine: str
    supports_vision: bool
    liveness_state: Literal["online", "offline", "unknown"]
    notices_awaiting_response: list[OpenNotice]
    unread_notice_count: int
    heartbeat_paused_until: datetime | None


class AgentListCompact(BaseModel):
    """The three fields rendered by ``ava agents ls``."""

    agent_id: int
    status: AgentStatus
    label: str | None


class AgentSnapshot(BaseModel):
    """Full lifecycle state of one agent.

    `status` values: running / idling / restarting / terminated.

    `spawner` shape: "user" / "agent:<id>" / arbitrary external name — the
    frontend builds a spawn tree from this.

    `label` is None when not set; the frontend falls back to `#N`.

    `last_active_at` is the agent's real-activity clock (agents_meta column,
    written on every completed LLM turn); `last_inbound_at` is the most
    recent inbound of any kind. They differ exactly for an agent working
    through a long single turn — last_active_at stays current while
    last_inbound_at goes stale (issue #183). Both fall back to
    started_at / spawned_at before any activity/inbound. The frontend sorts
    within status groups by last_active_at desc.

    `notices_awaiting_response` is the agent's open require_response notices,
    oldest first — empty when it is not waiting on the user.

    `supports_vision` describes the effective model: the per-agent
    `config_overlay.llm_model` when set, otherwise `settings.lm.llm_model`.
    It uses `shared.lm.factory.model_supports_vision`, the same capability
    lookup as the message API's image-content gate.
    """

    agent_id: int
    spawner: str
    fork_source_agent_id: int | None
    fork_source_checkpoint_id: str | None
    status: AgentStatus
    pid: int | None
    spawned_at: datetime
    started_at: datetime | None
    last_active_at: datetime
    last_inbound_at: datetime
    label: str | None
    machine: str
    supports_vision: bool
    # Gateway-owned liveness projection (Task #1174): 'online' = the machine is
    # reachable AND (for running/idling) the process lease is alive; 'offline'
    # = machine unreachable (2 consecutive failed status_probe) or lease
    # expired; 'unknown' = the gateway has not judged this row (fresh row /
    # unregistered machine) — rendered conservatively as online. Written only
    # by the gateway heartbeat daemon's liveness pass; `status` stays lifecycle
    # intent. `last_probe_at` = when that pass last judged this row.
    liveness_state: Literal["online", "offline", "unknown"]
    last_probe_at: datetime | None
    notices_awaiting_response: list[OpenNotice]
    unread_notice_count: int
    heartbeat_paused_until: datetime | None


def _row_to_snapshot(row: tuple[Any, ...]) -> AgentSnapshot:
    # Pydantic does the per-field type coercion / validation; the tuple
    # positions match the SELECT column order above.
    config_overlay = row[17]
    effective_model = (
        config_overlay["llm_model"]
        if config_overlay and "llm_model" in config_overlay
        else settings.lm.llm_model
    )
    return AgentSnapshot.model_validate(
        {
            "agent_id": row[0],
            "spawner": row[1],
            "fork_source_agent_id": row[2],
            "fork_source_checkpoint_id": row[3],
            "status": AgentStatus(row[4]),
            "pid": row[5],
            "spawned_at": row[6],
            "started_at": row[7],
            "last_active_at": row[8],
            "last_inbound_at": row[9],
            "label": row[10],
            "machine": row[11],
            "heartbeat_paused_until": row[12],
            "liveness_state": row[13],
            "last_probe_at": row[14],
            "notices_awaiting_response": row[15],
            "unread_notice_count": row[16],
            "supports_vision": model_supports_vision(effective_model),
        }
    )


def _row_to_summary(row: tuple[Any, ...]) -> AgentListSummary:
    effective_model = row[15] or settings.lm.llm_model
    return AgentListSummary.model_validate(
        {
            "agent_id": row[0],
            "spawner": row[1],
            "fork_source_agent_id": row[2],
            "status": AgentStatus(row[3]),
            "pid": row[4],
            "spawned_at": row[5],
            "started_at": row[6],
            "last_active_at": row[7],
            "last_inbound_at": row[8],
            "label": row[9],
            "machine": row[10],
            "heartbeat_paused_until": row[11],
            "liveness_state": row[12],
            "notices_awaiting_response": row[13],
            "unread_notice_count": row[14],
            "supports_vision": model_supports_vision(effective_model),
        }
    )


def _row_to_compact(row: tuple[Any, ...]) -> AgentListCompact:
    return AgentListCompact.model_validate(
        {
            "agent_id": row[0],
            "status": AgentStatus(row[1]),
            "label": row[2],
        }
    )


def select_one(conn: psycopg.Connection, agent_id: int) -> AgentSnapshot | None:
    """Look up a single agent's snapshot; returns None when the row does not exist."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_FULL_COLS} {_FROM} WHERE a.id = %s {_GROUP}",
            (agent_id,),
        )
        row = cur.fetchone()
    return _row_to_snapshot(row) if row else None


async def select_one_async(
    conn: psycopg.AsyncConnection[Any], agent_id: int
) -> AgentSnapshot | None:
    """`select_one` twin for async contexts (agent graph nodes)."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_FULL_COLS} {_FROM} WHERE a.id = %s {_GROUP}",
            (agent_id,),
        )
        row = await cur.fetchone()
    return _row_to_snapshot(row) if row else None


def select_all(
    conn: psycopg.Connection,
    *,
    scope: AgentListScope = "all",
    fields: AgentListFields = "full",
) -> list[AgentSnapshot] | list[AgentListSummary] | list[AgentListCompact]:
    """List full snapshots, roster summaries, or compact CLI rows for ``scope``.

    ``all`` preserves the historical SDK / ops contract.  Frontend roster
    readers request ``live`` so terminated history is excluded by Postgres
    before the snapshot's per-agent lookups run.  ``terminated`` is the
    explicit history surface. ``summary`` does not select full-only values;
    ``compact`` avoids every roster-only lookup for the CLI's three columns.
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT_ALL_SQL[fields][scope])
        rows = cur.fetchall()
    if fields == "summary":
        return [_row_to_summary(r) for r in rows]
    if fields == "compact":
        return [_row_to_compact(r) for r in rows]
    return [_row_to_snapshot(r) for r in rows]


class ActivityEntry(BaseModel):
    """One entry of an agent's self-reported activity trail (migration 0042).

    Historical only: the SDK write verb (`ava.self.log`) was removed
    2026-08-02, so no new rows appear; this backs the frozen
    GET /api/agents/{id}/activity endpoint.
    """

    text: str
    created_at: datetime


def select_activity_trail(conn: psycopg.Connection, agent_id: int) -> list[ActivityEntry]:
    """The agent's full activity trail, oldest first. Empty for an agent that
    has never reported (or a nonexistent agent — lenient read, no 404)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT text, created_at FROM agent_activity "
            "WHERE agent_id = %s ORDER BY created_at, id",
            (agent_id,),
        )
        rows = cur.fetchall()
    return [ActivityEntry(text=r[0], created_at=r[1]) for r in rows]
