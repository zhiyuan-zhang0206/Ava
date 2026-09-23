"""Bounded agent directory and live-tree reads.

Cards contain scalar state only. The live tree is one database statement:
current cards plus unique ancestor links, never unrelated terminated rows.
History uses descending-ID keyset pages; no implicit list-all operation exists.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg import sql
from pydantic import BaseModel

from shared.agent_observation import AgentObservation, observation
from shared.agents import AgentStatus
from shared.config import settings
from shared.lm.factory import model_supports_vision
from shared.lm.registry import resolve_available_model
from shared.tasks.priority import Priority

AgentDirectoryScope = Literal["live", "terminated", "all"]


class AgentLineage(BaseModel):
    """Immutable relationship facts needed to walk a visible node's ancestors."""

    agent_id: int
    spawner: str
    fork_source_agent_id: int | None


class AgentCard(AgentLineage):
    """Scalar list state; selected-agent details and notice bodies are separate reads."""

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
    observation: AgentObservation
    awaiting_response_count: int
    highest_notice_priority: Priority | None
    unread_notice_count: int
    heartbeat_paused_until: datetime | None
    open_impersonation_session_id: int | None


class AgentDirectoryPage(BaseModel):
    """One newest-first directory page; the cursor is the last returned agent ID."""

    agents: list[AgentCard]
    next_cursor: int | None


class AgentRoster(BaseModel):
    """A coherent live tree. Ancestors carry links, never historical agent details."""

    agents: list[AgentCard]
    ancestors: list[AgentLineage]


_CARD_COLUMNS = """
    a.id AS agent_id, a.spawner, a.fork_source_agent_id, a.status, a.pid,
    a.spawned_at, a.started_at,
    COALESCE(a.last_active_at, a.started_at, a.spawned_at) AS last_active_at,
    COALESCE(im.last_inbound_at, a.started_at, a.spawned_at) AS last_inbound_at,
    t.label, a.machine, a.heartbeat_paused_until, a.liveness_state,
    a.config_overlay ->> 'llm_model' AS effective_model,
    mp.last_probe_at AS machine_probe_at, a.lease_expires_at,
    attention.awaiting_response_count, attention.highest_notice_priority,
    fyi.unread_notice_count,
    open_impersonation.session_id AS open_impersonation_session_id
"""
_CARD_FROM = """
    FROM selected s
    JOIN agents_meta a ON a.id = s.id
    JOIN agents t ON t.id = a.id
    LEFT JOIN machine_probe mp ON mp.machine_name = a.machine
    LEFT JOIN LATERAL (
        SELECT MAX(created_at) AS last_inbound_at
        FROM inbound_messages WHERE agent_id = a.id
    ) im ON true
    LEFT JOIN LATERAL (
        SELECT count(*) AS awaiting_response_count, min(priority) AS highest_notice_priority
        FROM agent_notices
        WHERE agent_id = a.id AND require_response AND resolved_at IS NULL
    ) attention ON true
    LEFT JOIN LATERAL (
        SELECT count(*) AS unread_notice_count FROM agent_notices
        WHERE agent_id = a.id AND NOT require_response AND resolved_at IS NULL
          AND created_at > now() - interval '30 days'
    ) fyi ON true
    LEFT JOIN LATERAL (
        SELECT session_id FROM agent_impersonations
        WHERE agent_id = a.id AND status IN ('requested', 'accepted', 'active')
    ) open_impersonation ON true
"""
# UNION (not UNION ALL) deduplicates ancestors shared by many live agents and
# terminates even on corrupt cyclic links. Fork-source existence takes priority;
# an absent source falls back to the triggering agent, matching the tree model.
_LIVE_SQL = sql.SQL("""
    WITH RECURSIVE selected AS MATERIALIZED (
        SELECT id FROM agents_meta WHERE status <> 'terminated'
    ), lineage AS (
        SELECT a.id, a.spawner, a.fork_source_agent_id
        FROM agents_meta a JOIN selected s ON s.id = a.id
        UNION
        SELECT parent.id, parent.spawner, parent.fork_source_agent_id
        FROM lineage child
        JOIN agents_meta parent ON parent.id = COALESCE(
            (SELECT id FROM agents_meta WHERE id = child.fork_source_agent_id),
            CASE WHEN child.spawner ~ '^agent:[0-9]{{1,19}}$'
                 THEN CASE WHEN substring(child.spawner FROM 7)::numeric <= 9223372036854775807
                      THEN substring(child.spawner FROM 7)::bigint END END
        )
    ), cards AS (
        SELECT {columns} {source} ORDER BY a.id
    )
    SELECT
        COALESCE((SELECT json_agg(cards) FROM cards), '[]'::json),
        COALESCE((SELECT json_agg(links) FROM (
            SELECT id AS agent_id, spawner, fork_source_agent_id FROM lineage
            WHERE id NOT IN (SELECT id FROM selected) ORDER BY id
        ) links), '[]'::json)
""").format(columns=sql.SQL(_CARD_COLUMNS), source=sql.SQL(_CARD_FROM))


def _card(data: dict[str, Any]) -> AgentCard:
    model = resolve_available_model(data.pop("effective_model") or settings.lm.llm_model)
    data["supports_vision"] = model_supports_vision(model)
    probe = data.pop("machine_probe_at")
    lease = data.pop("lease_expires_at")
    data["observation"] = observation(
        datetime.fromisoformat(probe) if probe else None,
        datetime.fromisoformat(lease) if lease else None,
    )
    return AgentCard.model_validate(data)


def select_roster(conn: psycopg.Connection[Any]) -> AgentRoster:
    """Read live cards and their ancestor closure from the same database snapshot."""
    with conn.cursor() as cur:
        cur.execute(_LIVE_SQL)
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("roster aggregate did not return a row")
    return AgentRoster(
        agents=[_card(data) for data in row[0]],
        ancestors=[AgentLineage.model_validate(data) for data in row[1]],
    )


def list_directory(
    conn: psycopg.Connection[Any],
    *,
    scope: AgentDirectoryScope = "live",
    query: str = "",
    before_id: int | None = None,
    limit: int = 100,
) -> AgentDirectoryPage:
    """Read one bounded page; search matches a label substring or exact numeric ID."""
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    predicates = {
        "live": sql.SQL("a.status <> 'terminated'"),
        "terminated": sql.SQL("a.status = 'terminated'"),
        "all": sql.SQL("true"),
    }
    conditions = [predicates[scope]]
    params: list[Any] = []
    if before_id is not None:
        conditions.append(sql.SQL("a.id < %s"))
        params.append(before_id)
    if before_id is not None and not 1 <= before_id <= 9223372036854775807:
        raise ValueError("before_id must be a positive bigint agent ID")
    if len(query) > 200:
        raise ValueError("query must be at most 200 characters")
    query = query.strip()
    if query:
        conditions.append(
            sql.SQL("(position(lower(%s) in lower(COALESCE(t.label, ''))) > 0 OR a.id = %s)")
        )
        token = query.removeprefix("#")
        numeric_id = (
            int(token) if len(token) <= 19 and token.isascii() and token.isdecimal() else None
        )
        if numeric_id is not None and numeric_id > 9223372036854775807:
            numeric_id = None
        params.extend([query, numeric_id])
    params.append(limit + 1)
    statement = sql.SQL("""
        WITH selected AS MATERIALIZED (
            SELECT a.id FROM agents_meta a JOIN agents t ON t.id = a.id
            WHERE {where} ORDER BY a.id DESC LIMIT %s
        )
        SELECT row_to_json(cards) FROM (
            SELECT {columns} {source} ORDER BY a.id DESC
        ) cards
    """).format(
        where=sql.SQL(" AND ").join(conditions),
        columns=sql.SQL(_CARD_COLUMNS),
        source=sql.SQL(_CARD_FROM),
    )
    with conn.cursor() as cur:
        cur.execute(statement, params)
        rows = cur.fetchall()
    agents = [_card(row[0]) for row in rows[:limit]]
    return AgentDirectoryPage(
        agents=agents,
        next_cursor=agents[-1].agent_id if len(rows) > limit else None,
    )
