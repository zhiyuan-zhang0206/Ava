"""Agent-to-agent events — ``GET /api/insights/run-timeline/links?agents=405,6657&from=&to=``.

Every audit event of the kinds below that one agent (or the user) did to another, where either end is among the
asked agents, plus the notices the agents posted to the user. Who did it is the audit row's `source` (`agent:N`); the agent it was done to is the
row's `agent_id`. `target_agent_id` is not used: its direction differs per event (for a
send_message it is the sender, for a spawn the spawner, for a fork the agent copied from).
Events whose source is not an agent or the user (system, schedule...) are left out; a send_message counts only when its `inbound_id` names a `kind='chat'` inbound row (a task
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
    RunTimelineLinkContent,
    RunTimelineLinks,
)

router = APIRouter()

# The kinds read from `audit_events`; a notice comes from `agent_notices`.
_AUDIT_KINDS: list[str] = [kind for kind in get_args(LinkKind) if kind != "notice"]
# A transport page, not a display limit: the reader pages until the window is exhausted.
_PAGE_SIZE = 500
_AGENT_PREFIX = "agent:"
_HUMAN_SOURCES = ("user", "ui:page:")


def sender_of(source: str) -> tuple[bool, int | None]:
    """`(drawn, agent)` for an audit source: an agent, or the user (agent None); any other valid source is not drawn. Unknown prefixes raise."""
    validate_source(source)
    if source.startswith(_HUMAN_SOURCES):
        return True, None
    if source.startswith(_AGENT_PREFIX):
        return True, int(source.removeprefix(_AGENT_PREFIX))
    return False, None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _message_inbound(row: dict[str, object]) -> int | None:
    attrs = row["attributes"]
    return (
        _int(cast(dict[str, object], attrs).get("inbound_id")) if isinstance(attrs, dict) else None
    )


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
    drawn, sender = sender_of(str(row["source"]))
    receiver = _int(row["agent_id"])
    # An event an agent did to itself is not between agents.
    if not drawn or receiver is None or sender == receiver:
        return None
    kind = cast(LinkKind, row["event_name"])
    return RunTimelineLink(
        kind=kind,
        ts=cast(datetime, row["ts"]),
        sender=sender,
        receiver=receiver,
        inbound_id=_message_inbound(row),
        fork_from=_int(row["target_agent_id"]) if kind == "fork" else None,
        notice_id=None,
    )


def _notices(
    conn: psycopg.Connection, agents: list[int], start: datetime, end: datetime
) -> list[RunTimelineLink]:
    """Notices the agents posted to the user in the window: the one structured agent-to-user channel."""
    rows = conn.execute(
        "SELECT id, agent_id, created_at FROM agent_notices "
        "WHERE agent_id = ANY(%s) AND created_at >= %s AND created_at <= %s ORDER BY created_at, id",
        [agents, start, end],
    ).fetchall()
    return [
        RunTimelineLink(
            kind="notice",
            ts=created_at,
            sender=int(agent_id),
            receiver=None,
            inbound_id=None,
            fork_from=None,
            notice_id=int(notice_id),
        )
        for notice_id, agent_id, created_at in rows
    ]


def read(db: Database, agents: list[int], start: datetime, end: datetime) -> list[RunTimelineLink]:
    """The window's events between agents, with the user, with an end in `agents`, oldest first."""
    links: list[RunTimelineLink] = []
    offset = 0
    with db.connect(autocommit=True) as conn:
        while True:
            page, has_more = audit_rows.query_events(
                conn,
                involving_agents=agents,
                event_names=_AUDIT_KINDS,
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
                links.extend(_notices(conn, agents, start, end))
                return sorted(links, key=lambda link: link.ts)
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


@router.get("/api/insights/run-timeline/link-content")
def get_run_timeline_link_content(
    request: Request,
    inbound_id: Annotated[int | None, Query(ge=1)] = None,
    notice_id: Annotated[int | None, Query(ge=1)] = None,
) -> RunTimelineLinkContent:
    """The full text of one chat message (`inbound_id`) or the title and text of one notice (`notice_id`); exactly one."""
    if (inbound_id is None) == (notice_id is None):
        raise HTTPException(status_code=422, detail="give exactly one of inbound_id and notice_id")
    db: Database = request.app.state.db
    with db.connect(autocommit=True) as conn:
        if inbound_id is not None:
            row = conn.execute(
                "SELECT content FROM inbound_messages WHERE id = %s AND kind = 'chat'", [inbound_id]
            ).fetchone()
            found = None if row is None else RunTimelineLinkContent(title=None, content=str(row[0]))
        else:
            row = conn.execute(
                "SELECT title, content FROM agent_notices WHERE id = %s", [notice_id]
            ).fetchone()
            found = (
                None
                if row is None
                else RunTimelineLinkContent(title=str(row[0]), content=str(row[1] or ""))
            )
    if found is None:
        raise HTTPException(status_code=404, detail="no such message or notice")
    return found
