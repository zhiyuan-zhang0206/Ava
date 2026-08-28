"""The ava.ui.notify() agent_notices endpoints (user-facing).

- GET  /api/notices/open     — cross-fleet open FYI notices (require_response false).
- GET  /api/notices/escalations — open task escalations for operator review.
- GET  /api/notices/resolved — recently-resolved notices across the fleet (history).
- POST /api/agents/{id}/notices/{notice_id}/resolve — resolve one open notice.

The unified agent->user queue lives in agent_notices (migration 0053). An agent
calls ava.ui.notify() to post a notice at one of three rungs (require_response x
blocking). Notices that need a response ride the agent snapshot inline (a bounded
worklist); FYI notices (require_response false) stay off the snapshot — the
snapshot carries only unread_notice_count — so GET /api/notices/open is the FYI
feed's content source. Both kinds surface in the GET /api/notices/resolved
history once resolved.

A notice is resolved by the user (answer / dismiss / read) via the POST here, or
withdrawn by the agent via ava.ui.dismiss_notice(); either sets resolved_at +
resolution. When the user supplies a reply it is cached on the row (reply column)
and delivered to the agent as a chat inbound — the reply column is a cache, not
the delivery channel. Every notice resolution is a notice-system event, not
user speech: a user reply is delivered as `system:notice-reply` and a bare
dismiss as `system:notice-dismiss` — both system-sourced inbounds, so no
user-role message is consumed and nothing asks for a reply.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Annotated, Any, LiteralString, cast

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request, Response
from psycopg_pool import ConnectionPool

from gateway.routers._delivery import deliver_chat_inbound
from gateway.schemas import (
    AgentMessageEnqueued,
    EscalationNoticeItem,
    NoticeCreateIn,
    NoticeEditIn,
    NoticeItem,
    NoticesCursor,
    NoticesFeed,
    ResolveBatchIn,
    ResolveNoticeIn,
)
from ops import ops_lifecycle as _ops
from shared.db import NOTICE_FYI_TTL_DAYS

router = APIRouter()
_log = logging.getLogger(__name__)

_SELECT = (
    "SELECT n.id, n.agent_id, t.label, n.title, n.content, n.priority, "
    "n.require_response, n.blocking, n.created_at, n.updated_at, "
    "n.resolved_at, n.resolution, n.reply, n.task_id "
    "FROM agent_notices n JOIN agents t ON t.id = n.agent_id "
)

_ESCALATIONS_SELECT = (
    "SELECT n.id, n.title, n.priority, n.created_at, n.task_id, task.title, task.status, "
    "task.owner, owner.label, task.reminder_count, task.updated_at "
    "FROM agent_notices n "
    "JOIN agent_tasks task ON task.id = n.task_id "
    "LEFT JOIN agents owner ON owner.id = task.owner "
)

# action -> stored resolution. Explicit map (never inferred from reply presence).
_ACTION_RESOLUTION = {"answer": "answered", "dismiss": "dismissed", "read": "read"}


def _row_to_item(r: tuple[Any, ...]) -> NoticeItem:
    return NoticeItem(
        id=r[0],
        agent_id=r[1],
        agent_label=r[2],
        title=r[3],
        content=r[4],
        priority=r[5],
        require_response=r[6],
        blocking=r[7],
        created_at=r[8],
        updated_at=r[9],
        resolved_at=r[10],
        resolution=r[11],
        reply=r[12],
        task_id=r[13],
    )


def _row_to_escalation_item(r: tuple[Any, ...]) -> EscalationNoticeItem:
    return EscalationNoticeItem(
        id=r[0],
        title=r[1],
        priority=r[2],
        created_at=r[3],
        task_id=r[4],
        task_title=r[5],
        task_status=r[6],
        owner_id=r[7],
        owner_label=r[8],
        reminder_count=r[9],
        updated_at=r[10],
    )


def _open_notices_blocking(
    pool: ConnectionPool, limit: int, *, include_awaiting: bool
) -> list[tuple[Any, ...]]:
    """Sync open-notices query — via to_thread. FYI-only by default;
    include_awaiting=True returns both kinds (the IM bridge's queue view).

    Lazy expiry (audit C1): before the SELECT, FYI notices older than
    NOTICE_FYI_TTL_DAYS are auto-resolved 'read' in the same transaction — so
    the open feed, the resolved history, and every later query agree. The
    query-side cutoff below is belt-and-braces for readers that skip the
    sweep (require_response notices never expire)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_notices SET resolved_at = now(), resolution = 'read' "
            "WHERE NOT require_response AND resolved_at IS NULL "
            "AND created_at <= now() - make_interval(days => %s)",
            (NOTICE_FYI_TTL_DAYS,),
        )
        where = (
            "WHERE n.resolved_at IS NULL AND (n.require_response OR "
            "n.created_at > now() - make_interval(days => %s)) "
            if include_awaiting
            else "WHERE NOT n.require_response AND n.resolved_at IS NULL "
            "AND n.created_at > now() - make_interval(days => %s) "
        )
        cur.execute(
            _SELECT + where + "ORDER BY n.priority ASC, n.created_at DESC, n.id DESC LIMIT %s",
            (NOTICE_FYI_TTL_DAYS, limit),
        )
        return cur.fetchall()


def _escalation_notices_blocking(pool: ConnectionPool) -> list[tuple[Any, ...]]:
    """Sync open task-escalations query — via to_thread, without FYI expiry."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            _ESCALATIONS_SELECT
            + "WHERE n.require_response AND n.task_id IS NOT NULL AND n.resolved_at IS NULL "
            + "ORDER BY n.priority ASC, n.created_at DESC, n.id DESC"
        )
        return cur.fetchall()


def _resolved_notices_blocking(
    pool: ConnectionPool, sql: str, params: list[object]
) -> list[tuple[Any, ...]]:
    """Sync resolved-history query — via to_thread."""
    with pool.connection() as conn, conn.cursor() as cur:
        # The composed query is assembled from fixed literal fragments + typed
        # params only; psycopg's static type demands LiteralString.
        cur.execute(cast(LiteralString, sql), params)
        return cur.fetchall()


@router.get("/api/notices/open")
async def get_open_notices(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    include_awaiting: Annotated[bool, Query()] = False,  # noqa: FBT002 — FastAPI query param
) -> list[NoticeItem]:
    """The cross-fleet open notices feed, priority-then-newest.

    By default only FYI notices (require_response false) — response-required
    ones ride the agent snapshot. include_awaiting=True returns both kinds:
    the IM bridge's "/notice list" queue view (Task #941) lists everything
    still open and hands each item its own processing buttons. A notice is
    open when resolved_at IS NULL. `limit` caps a runaway backlog.
    """
    rows = await asyncio.to_thread(
        _open_notices_blocking, request.app.state.db_pool, limit, include_awaiting=include_awaiting
    )
    return [_row_to_item(r) for r in rows]


@router.get("/api/notices/escalations")
async def get_escalation_notices(request: Request) -> list[EscalationNoticeItem]:
    """Open task escalations with their task and current-owner review context.

    This is the operator's read-only queue: only response-required notices that
    name a task are included. Escalations never run the FYI lazy-expiry sweep.
    """
    rows = await asyncio.to_thread(_escalation_notices_blocking, request.app.state.db_pool)
    return [_row_to_escalation_item(r) for r in rows]


def _notices_after_blocking(pool: ConnectionPool, after: int, limit: int) -> list[tuple[Any, ...]]:
    """Sync new-open query (both kinds) — via to_thread.

    Expired FYI notices (older than NOTICE_FYI_TTL_DAYS) are excluded — the
    IM bridge's cursor must not stall on or re-deliver notices the open feed
    already auto-resolved (audit C1). require_response notices never expire."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            _SELECT + "WHERE n.id > %s AND n.resolved_at IS NULL AND (n.require_response OR "
            "n.created_at > now() - make_interval(days => %s)) " + "ORDER BY n.id ASC LIMIT %s",
            (after, NOTICE_FYI_TTL_DAYS, limit),
        )
        return cur.fetchall()


@router.get("/api/notices/live")
async def get_notices_live(
    request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[NoticeItem]:
    """New open notices across the fleet, both kinds, oldest-first by id.

    The IM bridge polls this with the max id it has seen and pushes new
    notices to Telegram (Task #884) — one query, no event dependency, and it
    covers require_response notices (which publish no NoticePosted event) as
    well as FYIs. Idempotent: rows are only ever returned while open, so a
    poll after a notice was resolved simply stops seeing it.
    """
    rows = await asyncio.to_thread(_notices_after_blocking, request.app.state.db_pool, after, limit)
    return [_row_to_item(r) for r in rows]


@router.get("/api/notices/resolved")
async def get_resolved_notices(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    require_response: Annotated[bool | None, Query()] = None,
    before_at: Annotated[datetime | None, Query()] = None,
    before_id: Annotated[int | None, Query()] = None,
) -> list[NoticeItem]:
    """One page of resolved notices across the fleet, newest resolution first.

    Feeds the greyed history beneath each open queue. `require_response` filters to
    one queue's history (the "needs response" tab passes true, the FYI tab false);
    omit it for both. Keyset-paginated on (resolved_at, id): pass the last row's
    (before_at, before_id) for the next page strictly older. Supply both or neither.
    """
    if (before_at is None) != (before_id is None):
        raise HTTPException(
            status_code=422, detail="before_at and before_id must be supplied together"
        )
    where = "WHERE n.resolved_at IS NOT NULL "
    params: list[object] = []
    if require_response is not None:
        where += "AND n.require_response = %s "
        params.append(require_response)
    if before_at is not None:
        where += "AND (n.resolved_at, n.id) < (%s, %s) "
        params.extend([before_at, before_id])
    params.append(limit)
    rows = await asyncio.to_thread(
        _resolved_notices_blocking,
        request.app.state.db_pool,
        _SELECT + where + "ORDER BY n.resolved_at DESC, n.id DESC LIMIT %s",
        params,
    )
    return [_row_to_item(r) for r in rows]


@router.get("/api/notices")
async def get_notices_feed(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    resolved_limit: Annotated[int, Query(ge=1, le=100)] = 30,
    before_at: Annotated[datetime | None, Query()] = None,
    before_id: Annotated[int | None, Query()] = None,
) -> NoticesFeed:
    """The unified inbox feed (Task #1024, R4 layer 2, decision Q1=A): one
    request carries the whole Inbox panel.

    - `open` — cross-fleet open FYI notices (require_response false),
      priority then newest. Same content as GET /api/notices/open.
    - `awaiting` — cross-fleet open require_response notices, same sort.
      Today these ride the agent snapshot (notices_awaiting_response); the
      merged endpoint makes them a first-class pipe so the frontend's Inbox
      no longer merges three sources.
    - `resolved_page` + `next_cursor` — one keyset page of the resolved
      history (both kinds, newest resolution first), same semantics as
      GET /api/notices/resolved: pass next_cursor back as before_at /
      before_id for the next strictly-older page; None means the end.

    The standalone endpoints stay for their other consumers (IM bridge,
    CLI). The open sweep (FYI TTL auto-resolve) runs once per call, so the
    open list, the awaiting list and the history agree within one request.
    """
    if (before_at is None) != (before_id is None):
        raise HTTPException(
            status_code=422, detail="before_at and before_id must be supplied together"
        )
    pool = request.app.state.db_pool
    resolved_where = "WHERE n.resolved_at IS NOT NULL "
    resolved_params: list[object] = []
    if before_at is not None:
        resolved_where += "AND (n.resolved_at, n.id) < (%s, %s) "
        resolved_params.extend([before_at, before_id])
    resolved_params.append(resolved_limit)
    open_rows, awaiting_rows, resolved_rows = await asyncio.gather(
        asyncio.to_thread(_open_notices_blocking, pool, limit, include_awaiting=False),
        asyncio.to_thread(_open_notices_blocking, pool, limit, include_awaiting=True),
        asyncio.to_thread(
            _resolved_notices_blocking,
            pool,
            _SELECT + resolved_where + "ORDER BY n.resolved_at DESC, n.id DESC LIMIT %s",
            resolved_params,
        ),
    )
    # The include_awaiting=True query returns both kinds merged; the awaiting
    # list is exactly the require_response half (column index 6, see _SELECT).
    awaiting = [r for r in awaiting_rows if r[6]]
    resolved_page = [_row_to_item(r) for r in resolved_rows]
    next_cursor = None
    if len(resolved_page) == resolved_limit:
        last = resolved_page[-1]
        if last.resolved_at is not None:
            next_cursor = NoticesCursor(before_at=last.resolved_at, before_id=last.id)
    return NoticesFeed(
        open=[_row_to_item(r) for r in open_rows],
        awaiting=[_row_to_item(r) for r in awaiting],
        resolved_page=resolved_page,
        next_cursor=next_cursor,
    )


@router.post("/api/agents/{agent_id}/notices/{notice_id}/resolve", status_code=201)
async def post_notice_resolve(
    agent_id: int, notice_id: int, body: ResolveNoticeIn, request: Request
) -> AgentMessageEnqueued:
    """Resolve one open notice and (when a reply is given) wake the agent with it.

    `action` is the explicit close verb: answer applies to both kinds — the
    user can reply to an FYI too (Task #1061, the reply reaches the notice's
    agent like a require_response answer); dismiss applies to a require_response
    notice, read to an FYI notice. The endpoint marks the notice resolved
    (resolved_at + resolution + cached reply) iff it is still open, then — when a
    reply is supplied, or a require_response notice is dismissed — INSERTs a
    self-describing chat inbound so the agent knows which notice this answers and
    (for a dismissed blocking notice) stops waiting. The inbound is system-sourced
    (`system:notice-reply` / `system:notice-dismiss`), rendered `[system] ...`
    with no User envelope: a notice resolution is a notice-system event, not user
    speech, so no user-role message is consumed and nothing is shaped like a
    reply request.

    409 when the notice does not exist, or `action` does not match the notice's
    kind (dismiss on an FYI notice, or read on one needing a response). A bare
    `read` on an already-resolved notice is an idempotent success (user ruling
    2026-08-28: "Mark read" means "I have read it", so the already-read state
    ends silently, like the normal path — the same skip-not-error semantics as
    the batch endpoint); a note attached to that read still reaches the agent,
    and `answer` / `dismiss` on an already-resolved notice keep the 409 so a
    reply or a dismissal is never silently dropped. 422 when `action` is
    `answer` without a reply.
    """
    action = body.action
    reply = body.reply

    def _resolve(conn: psycopg.Connection) -> str | None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title, require_response, resolved_at FROM agent_notices "
                "WHERE id = %s AND agent_id = %s FOR UPDATE",
                (notice_id, agent_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"notice {notice_id} does not exist for agent {agent_id}",
                )
            title, require_response, resolved_at = row
            if resolved_at is not None:
                # Idempotent close: a bare 'read' on an already-resolved notice
                # silently succeeds (user ruling 2026-08-28) — the already-read
                # state ends like the normal path, matching the batch endpoint's
                # skip-not-error semantics. A note attached to that read still
                # reaches the agent (the close itself is a no-op); 'answer' and
                # 'dismiss' keep the 409 so a reply or a dismissal is never
                # silently dropped.
                if action == "read":
                    return f'Re: "{title}"\n\n{reply}' if reply is not None else None
                raise HTTPException(
                    status_code=409,
                    detail=f"notice {notice_id} is already resolved for agent {agent_id}",
                )
            if action == "dismiss" and not require_response:
                raise HTTPException(
                    status_code=409,
                    detail=f"action 'dismiss' applies to a notice needing a response; "
                    f"notice {notice_id} is FYI (use 'answer' or 'read')",
                )
            if action == "read" and require_response:
                raise HTTPException(
                    status_code=409,
                    detail=f"action 'read' applies to an FYI notice; "
                    f"notice {notice_id} needs a response (use 'answer' or 'dismiss')",
                )
            if action == "answer" and reply is None:
                raise HTTPException(status_code=422, detail="action 'answer' requires a reply")
            cur.execute(
                "UPDATE agent_notices SET resolved_at = now(), resolution = %s, reply = %s "
                "WHERE id = %s",
                (_ACTION_RESOLUTION[action], reply, notice_id),
            )
        if reply is not None:
            return f'Re: "{title}"\n\n{reply}'
        if action == "dismiss":
            return (
                f'Re: "{title}"\n\n[This notice was dismissed by the user — no answer will arrive.]'
            )
        return None  # read with no reply: nothing to deliver

    # Delivery channel: every notice resolution rides the notice-system source —
    # a user reply as `system:notice-reply`, a bare dismiss as
    # `system:notice-dismiss`. The claim node envelope-wraps both as "[system] ..."
    # (shared/envelope.py), so no User-role message is consumed and nothing is
    # shaped like a reply request; read-without-reply still delivers nothing.
    deliver_source = "system:notice-reply" if reply is not None else "system:notice-dismiss"
    delivery = await deliver_chat_inbound(
        request.app.state.db_pool,
        agent_id,
        prepare=_resolve,
        source=deliver_source,
        refresh_badge=True,
    )
    # Drop the row from the FYI feed (no-op by id for a require_response notice,
    # which lives on the snapshot, not the feed).
    await _ops.publish_notice_resolved(agent_id, notice_id)
    return AgentMessageEnqueued(status=delivery.status, inbound_id=delivery.inbound_id)


@router.post("/api/notices/resolve-batch")
async def post_notices_resolve_batch(body: ResolveBatchIn, request: Request) -> dict:
    """Mark many open FYI notices 'read' in one transaction (the Inbox's
    "Mark all read" — Task #1814).

    The single-notice resolve path (POST /api/agents/{id}/notices/{notice_id}/resolve)
    costs one HTTP round trip + one publish per notice, and each published
    notice_resolved refetches the whole open queue — clearing a pile of FYI
    notices one by one is N requests and N queue refetches. This endpoint does
    the whole pile in ONE transaction and returns before the UI reconciles.

    Batch semantics are deliberately permissive (idempotent): a row that is
    already resolved, or that needs a response, is skipped — counted in
    `skipped`, never an error — so a race with another resolver cannot fail
    the whole request. The batch action is always 'read' (an FYI close); a
    require_response notice can only be answered or dismissed one at a time.
    No replies are possible here (bare reads wake no agent).

    Returns {"resolved": n, "skipped": m}. 422 on an empty or oversized list.
    """

    ids = list(dict.fromkeys(body.notice_ids))  # dedupe, keep order
    if not ids:
        raise HTTPException(status_code=422, detail="notice_ids must be non-empty")
    if len(ids) > 500:
        raise HTTPException(status_code=422, detail="notice_ids must be at most 500")

    def _resolve_many(pool: ConnectionPool) -> list[tuple[int, int]]:
        """Resolve every listed FYI notice that is still open; return the
        (agent_id, notice_id) pairs to announce. One transaction, ONE
        statement — the WHERE clause IS the skip filter (already-resolved and
        require_response rows never match), so the whole batch is a single
        UPDATE ... RETURNING instead of a per-row select+update loop."""
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_notices SET resolved_at = now(), resolution = 'read' "
                "WHERE id = ANY(%s) AND resolved_at IS NULL AND NOT require_response "
                "RETURNING agent_id, id",
                (ids,),
            )
            return [(int(agent_id), int(notice_id)) for agent_id, notice_id in cur.fetchall()]

    pool = request.app.state.db_pool
    resolved_pairs = await asyncio.to_thread(_resolve_many, pool)
    # Announce after the commit, one event per resolved row — the frontend fold
    # drops them from the open queue (and its burst collapses into one refetch).
    for agent_id, notice_id in resolved_pairs:
        await _ops.publish_notice_resolved(agent_id, notice_id)
    return {"resolved": len(resolved_pairs), "skipped": len(ids) - len(resolved_pairs)}


# ── unified notice write API (R3 door ④) ─────────────────────────────────
# The agent->user queue has ONE write path now: the agent SDK calls these
# endpoints (ava.ui.notify / edit_notice / dismiss_notice), which perform the
# whole lifecycle atomically in one transaction — supersede+insert for
# notify, guarded update for edit/dismiss — plus the event publishes. The
# previous SDK-side direct-DB writes are gone; the agent_notices table is the
# single truth source and every writer goes through the same code.
#
# Pause semantics: data-plane like every other agent write — the quiesce has
# drained the agents before the migration window, so nothing legitimately
# writes notices during pause.


@router.post("/api/agents/{agent_id}/notices", status_code=201)
async def post_notice_create(agent_id: int, body: NoticeCreateIn, request: Request) -> dict:
    """Post a notice at one of the three obligation rungs (FYI /
    response-required / blocking), atomically superseding the agent's
    previous open notice.

    One transaction: every open notice of the agent is resolved as
    'superseded' (with a resolve event each), then the new row is inserted.
    Mirrors the previous SDK-side logic exactly — same supersede semantics,
    same local_id allocation, same events — now executed server-side so the
    two writes cannot tear.

    Returns the SDK Notice shape: the new notice's local id, the pending
    count + titles (for the SDK's pending list), and the superseded ids.
    """
    if not body.title.strip():
        raise HTTPException(status_code=422, detail="title must be non-empty")
    if body.blocking and not body.require_response:
        raise HTTPException(
            status_code=422,
            detail="blocking=True requires require_response=True (an FYI never stalls)",
        )

    def _create(pool: ConnectionPool) -> tuple[int, int, list[int], list[int]]:
        if body.task_id is not None:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1 FROM agent_tasks WHERE id = %s", (body.task_id,))
                if cur.fetchone() is None:
                    raise HTTPException(
                        status_code=422, detail=f"task {body.task_id} does not exist"
                    )
        with pool.connection() as conn, conn.cursor() as cur:
            # Auto-resolve any existing open notice (supersede).
            cur.execute(
                "SELECT id, local_id FROM agent_notices "
                "WHERE agent_id = %s AND resolved_at IS NULL FOR UPDATE",
                (agent_id,),
            )
            existing = cur.fetchall()
            superseded_local: list[int] = []
            superseded_global: list[int] = []
            for global_id, local_id in existing:
                cur.execute(
                    "UPDATE agent_notices SET resolved_at = now(), resolution = 'superseded' "
                    "WHERE id = %s",
                    (global_id,),
                )
                # Two id spaces, two consumers: the SDK's Notice.superseded
                # carries LOCAL ids (its id space); the published events must
                # carry GLOBAL ids (the frontend feed matches on those).
                # Passing a local id to publish_notice_resolved made the
                # frontend unable to drop the superseded notice until the next
                # snapshot refresh (audit cc-backend-runtime P2).
                superseded_local.append(int(local_id))
                superseded_global.append(int(global_id))
            # Insert the new notice with the agent's next local id.
            cur.execute(
                "INSERT INTO agent_notices "
                "(agent_id, local_id, title, content, priority, require_response, blocking, task_id) "
                "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices "
                "WHERE agent_id = %s), -1) + 1, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, local_id",
                (
                    agent_id,
                    agent_id,
                    body.title,
                    body.content,
                    body.priority,
                    body.require_response,
                    body.blocking,
                    body.task_id,
                ),
            )
            row = cur.fetchone()
            if row is None:  # pragma: no cover — RETURNING always yields
                raise RuntimeError("notice insert returned no row")
            return int(row[0]), int(row[1]), superseded_global, superseded_local

    def _pending(pool: ConnectionPool) -> list[dict[str, object]]:
        # Pending summary (the SDK's Notice carries it for the agent's view).
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT local_id AS id, title, created_at, priority "
                "FROM agent_notices WHERE agent_id = %s AND resolved_at IS NULL "
                "ORDER BY created_at DESC, local_id DESC",
                (agent_id,),
            )
            return [
                {"id": int(r[0]), "title": r[1], "created_at": r[2].isoformat(), "priority": r[3]}
                for r in cur.fetchall()
            ]

    pool = request.app.state.db_pool
    (
        notice_global_id,
        notice_local_id,
        superseded_global,
        superseded_local,
    ) = await asyncio.to_thread(_create, pool)
    # Events after the commit: superseded notices first (the feed drops them),
    # then the new posted notice. Best-effort, never raises. Events publish by
    # GLOBAL id; the SDK's return value carries LOCAL ids.
    for _gid in superseded_global:
        await _ops.publish_notice_resolved(agent_id, _gid)
    await _ops.publish_notice_posted(
        agent_id, notice_global_id, body.priority, body.title, body.task_id
    )
    pending = await asyncio.to_thread(_pending, pool)
    return {
        "id": int(notice_local_id),
        "pending_count": len(pending),
        "pending_notices": pending,
        "superseded": superseded_local,
    }


@router.patch("/api/agents/{agent_id}/notices/current", status_code=204, response_model=None)
async def patch_notice_edit(agent_id: int, body: NoticeEditIn, request: Request) -> Response:
    """Revise the agent's open notice (the SDK ava.ui.edit_notice contract).

    Pass only the fields to change; require_response cannot change (turn an
    FYI into a question by dismissing and posting fresh). No open notice ->
    idempotent 204 (the SDK contract: silently return). The posted event is
    re-published so the frontend feed picks up the revision.
    """
    # `model_fields_set` distinguishes "not passed" from "explicitly null":
    # the SDK contract clears a field by passing null (content=None erases the
    # body), and leaves it untouched by omitting it.
    sets: list[str] = []
    params: list[object] = []
    if "title" in body.model_fields_set:
        if not body.title or not body.title.strip():
            raise HTTPException(status_code=422, detail="title must be non-empty")
        sets.append("title = %s")
        params.append(body.title)
    if "content" in body.model_fields_set:
        sets.append("content = %s")
        params.append(body.content)  # None clears
    if "priority" in body.model_fields_set:
        sets.append("priority = %s")
        params.append(body.priority)
    if "blocking" in body.model_fields_set:
        sets.append("blocking = %s")
        params.append(body.blocking)
    if not sets:
        raise HTTPException(status_code=422, detail="edit needs at least one field to change")

    def _edit(conn: psycopg.Connection) -> tuple[int, bool, str, str, int | None] | None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, require_response, priority, title, task_id FROM agent_notices "
                "WHERE agent_id = %s AND resolved_at IS NULL FOR UPDATE",
                (agent_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            global_id, require_response, cur_priority, cur_title, cur_task_id = row
            if body.blocking is not None and body.blocking and not require_response:
                raise HTTPException(
                    status_code=422,
                    detail="blocking=True requires a notice that needs a response",
                )
            cur.execute(
                cast(
                    LiteralString,
                    "UPDATE agent_notices SET " + ", ".join(sets) + ", updated_at = now() "  # noqa: S608 — fixed literals only
                    "WHERE id = %s",
                ),
                (*params, global_id),
            )
            return (
                int(global_id),
                bool(require_response),
                str(body.priority if body.priority is not None else cur_priority),
                str(body.title if body.title is not None else cur_title),
                int(cur_task_id) if cur_task_id is not None else None,
            )

    def _edit_wrapper(pool: ConnectionPool) -> tuple[int, bool, str, str, int | None] | None:
        with pool.connection() as conn:
            edited = _edit(conn)
            conn.commit()
            return edited

    edited = await asyncio.to_thread(_edit_wrapper, request.app.state.db_pool)
    if edited is None:
        return Response(status_code=204)
    _gid, _require_response, new_priority, new_title, task_id = edited
    await _ops.publish_notice_posted(agent_id, _gid, new_priority, new_title, task_id)
    return Response(status_code=204)


@router.post("/api/agents/{agent_id}/notices/current/dismiss", status_code=204, response_model=None)
async def post_notice_dismiss(agent_id: int, request: Request) -> Response:
    """Withdraw the agent's open notice (the SDK ava.ui.dismiss_notice
    contract): resolution 'withdrawn' + resolve event. No open notice ->
    idempotent 204."""

    def _dismiss(pool: ConnectionPool) -> int | None:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_notices SET resolved_at = now(), resolution = 'withdrawn' "
                "WHERE agent_id = %s AND resolved_at IS NULL RETURNING id",
                (agent_id,),
            )
            row = cur.fetchone()
            dismissed = int(row[0]) if row is not None else None
            conn.commit()
            return dismissed

    dismissed = await asyncio.to_thread(_dismiss, request.app.state.db_pool)
    if dismissed is None:
        return Response(status_code=204)
    await _ops.publish_notice_resolved(agent_id, dismissed)
    return Response(status_code=204)
