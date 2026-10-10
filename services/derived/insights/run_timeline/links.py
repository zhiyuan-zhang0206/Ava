"""Agent-to-agent events — ``GET /api/insights/run-timeline/links?agents=405,6657&from=&to=``.

Every audit event of the kinds below that one agent did to another, where either end is among the
asked agents. Who did it is the audit row's `source` (`agent:N`); the agent it was done to is the
row's `agent_id`. `target_agent_id` is not used: its direction differs per event (for a
send_message it is the sender, for a spawn the spawner, for a fork the agent copied from).
Events whose source is not an agent (user, system, schedule...) are not agent-to-agent and are
left out; a send_message counts only when its `inbound_id` names a `kind='chat'` inbound row (a task
assignment is a system note) -- read per page against `inbound_messages`; a source with an unknown prefix is an error.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, cast, get_args

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request

from base.agents.messages.envelope import validate_source
from base.db import Database
from base.events.reads import audit_rows
from services.derived.insights.run_timeline.schemas import (
    LinkKind,
    RunTimelineLink,
    RunTimelineLinks,
)

router = APIRouter()

_KINDS: list[str] = list(get_args(LinkKind))
# A transport page, not a display limit: the reader pages until the window is exhausted.
_PAGE_SIZE = 500
_PREVIEW_CHARS = 160
_AGENT_PREFIX = "agent:"


def sender_of(source: str) -> int | None:
    """The agent a source names; None for a valid source that is not an agent. Unknown prefixes raise."""
    validate_source(source)
    if not source.startswith(_AGENT_PREFIX):
        return None
    return int(source.removeprefix(_AGENT_PREFIX))


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _message_inbound(row: dict[str, object]) -> int | None:
    attrs = row["attributes"]
    return _int(attrs.get("inbound_id")) if isinstance(attrs, dict) else None


def _chat_inbounds(conn: psycopg.Connection, page: list[dict[str, Any]]) -> set[int]:
    """The ids among the page's messages whose inbound row is a chat (a task assignment is a system note)."""
    ids = [
        inbound
        for row in page
        if row["event_name"] == "send_message" and (inbound := _message_inbound(row)) is not None
    ]
    if not ids:
        return set()
    rows = conn.execute(
        "SELECT id FROM inbound_messages WHERE id = ANY(%s) AND kind = 'chat'", [ids]
    ).fetchall()
    return {int(r[0]) for r in rows}


def _link(row: dict[str, object]) -> RunTimelineLink | None:
    sender = sender_of(str(row["source"]))
    receiver = _int(row["agent_id"])
    # An event an agent did to itself is not between agents.
    if sender is None or receiver is None or sender == receiver:
        return None
    raw = row["attributes"]
    attrs = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
    kind = cast(LinkKind, row["event_name"])
    content = attrs.get("content")
    return RunTimelineLink(
        kind=kind,
        ts=cast(datetime, row["ts"]),
        sender=sender,
        receiver=receiver,
        inbound_id=_int(attrs.get("inbound_id")) if kind == "send_message" else None,
        fork_from=_int(row["target_agent_id"]) if kind == "fork" else None,
        preview=" ".join(content.split())[:_PREVIEW_CHARS] if isinstance(content, str) else None,
    )


def read(db: Database, agents: list[int], start: datetime, end: datetime) -> list[RunTimelineLink]:
    """The window's agent-to-agent events with an end in `agents`, oldest first."""
    links: list[RunTimelineLink] = []
    offset = 0
    with db.connect(autocommit=True) as conn:
        while True:
            page, has_more = audit_rows.query_events(
                conn,
                involving_agents=agents,
                event_names=_KINDS,
                from_=start,
                to=end,
                limit=_PAGE_SIZE,
                offset=offset,
                direction="forward",
            )
            chats = _chat_inbounds(conn, page)
            links.extend(
                link
                for row in page
                if (row["event_name"] != "send_message" or _message_inbound(row) in chats)
                and (link := _link(row)) is not None
            )
            if not has_more:
                return links
            offset += _PAGE_SIZE


@router.get("/api/insights/run-timeline/links")
def get_run_timeline_links(
    request: Request,
    agents: Annotated[str, Query()],
    from_: Annotated[datetime, Query(alias="from")],
    to: Annotated[datetime, Query()],
) -> RunTimelineLinks:
    """Agent-to-agent events in the window with at least one end among the comma-separated `agents`."""
    try:
        ids = sorted({int(part) for part in agents.split(",")})
    except ValueError:
        raise HTTPException(
            status_code=422, detail="agents must be comma-separated integers"
        ) from None
    if from_.tzinfo is None or to.tzinfo is None:
        raise HTTPException(status_code=422, detail="from and to must include a timezone offset")
    if from_ >= to:
        raise HTTPException(status_code=422, detail="from must be earlier than to")
    db: Database = request.app.state.db
    return RunTimelineLinks(links=read(db, ids, from_, to))
