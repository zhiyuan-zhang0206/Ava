"""Agent message + state endpoints — /api/agents/* read/inspect surface.

Message delivery (POST /messages) + history reads (GET /messages, trace
checkpoint messages, last-message), the pending-inbound queue, activity
trail, token usage, and the context-breakdown panel. CRUD + spawn live in
routers/agents.py; the lifecycle surface in routers/agents_lifecycle.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from psycopg_pool import ConnectionPool

from gateway.routers._delivery import deliver_chat_inbound, reconcile_chat_delivery
from gateway.routers._eval_guard import caller_eval_isolation, deny_isolated_result_read
from gateway.schemas import (
    AgentMessageEnqueued,
    AgentMessagesResponse,
    ContextBreakdownResponse,
    ContextCategory,
    ContextSection,
    LastMessageResponse,
    PendingInbound,
    SystemNoteIn,
    TokenUsageResponse,
    TraceCheckpointMessagesResponse,
)
from ops import ops_lifecycle as _ops
from ops.agents import get_agent_status
from ops.rpc_schemas import AgentMessageIn, ContentBlock, ImageUrlContentBlock, TextContentBlock
from shared import agent_snapshot
from shared.chat_delivery import ClientMessageConflictError
from shared.checkpoint import (
    CheckpointReadError,
    load_checkpoint_messages,
    load_checkpoint_messages_by_trace,
)
from shared.config import settings
from shared.db import agent_exists, insert_inbound_message, list_pending_inbounds
from shared.db_transaction import write_transaction
from shared.inbound import InboundKind
from shared.lm.content import content_blocks
from shared.lm.factory import model_supports_vision, vision_capable_provider_names
from shared.uploads import image_mime_for, parse_upload_url, resolve_upload_path

router = APIRouter()
_log = logging.getLogger(__name__)

_MAX_MESSAGE_CONTENT_BYTES = 1_048_576
_DEFAULT_MESSAGE_LIMIT = 100

# Compatibility re-export for callers and tests that used this lookup before
# result-read enforcement was centralized.
__all__ = ["caller_eval_isolation"]


def _resolve_agent_model(request: Request, agent_id: int) -> str:
    """The agent's effective LLM model — its per-agent overlay, else the cluster
    default (`settings.lm.llm_model`). Same lookup as the token-usage endpoint."""
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    overlay = row[0] if row and row[0] else None
    model = overlay.get("llm_model") if overlay else None
    return model or settings.lm.llm_model


def _validate_image_ref(agent_id: int, url: str) -> None:
    """422/404 a multimodal image reference that is not a real image upload of
    this agent — a client cannot smuggle an arbitrary path/url into the model."""
    parsed = parse_upload_url(url)
    if parsed is None:
        raise HTTPException(422, f"image_url {url!r} is not an upload reference")
    ref_agent, name = parsed
    if ref_agent != agent_id:
        raise HTTPException(422, f"image_url {url!r} references a different agent's upload")
    if image_mime_for(name) is None:
        raise HTTPException(422, f"image_url {url!r} is not a recognized image type")
    try:
        path = resolve_upload_path(agent_id, name)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if not path.is_file():
        raise HTTPException(404, f"image upload {name!r} not found; upload it first")


def _prepare_message_content(
    request: Request, agent_id: int, content: str | list[ContentBlock]
) -> tuple[str, dict[str, object] | None]:
    """Split a message body into (text, payload) for storage.

    A plain string passes through unchanged (`text`, None). A block list is
    gated + validated, then split into the text part (stored in the `content`
    column for the envelope / timeline / legacy readers) plus a
    `{"content_blocks": [...]}` JSONB payload the claim node inlines as native
    model content. The capability gate 422s an image addressed to a text-only
    model here, up front, rather than letting the LLM call fail after the
    inbound is already queued.
    """
    if isinstance(content, str):
        return content, None

    blocks = content
    if any(isinstance(b, ImageUrlContentBlock) for b in blocks):
        model = _resolve_agent_model(request, agent_id)
        if not model_supports_vision(model):
            raise HTTPException(
                422,
                f"agent {agent_id}'s model {model!r} cannot see images — "
                "switch it to a vision-capable model "
                f"({', '.join(vision_capable_provider_names())})",
            )
        for b in blocks:
            if isinstance(b, ImageUrlContentBlock):
                _validate_image_ref(agent_id, b.image_url.url)

    return _normalize_message_content(content)


def _normalize_message_content(
    content: str | list[ContentBlock],
) -> tuple[str, dict[str, object] | None]:
    """Map the validated wire body to its immutable database representation.

    Unlike `_prepare_message_content`, this pure step does not re-read the
    agent's current model or uploaded files. Reconciliation must compare the
    original body with the durable row even if those mutable facts changed
    after the first POST committed.
    """
    if isinstance(content, str):
        return content, None
    text = "\n".join(
        block.text
        for block in content
        if isinstance(block, TextContentBlock) and block.text.strip()
    )
    # An image-only message stores a placeholder so the content column / envelope
    # / timeline text is never empty; the image conveys the real content.
    text = text or "[image]"
    payload: dict[str, object] = {"content_blocks": [block.model_dump() for block in content]}
    return text, payload


def _message_content_size_bytes(content: str | list[ContentBlock]) -> int:
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    wire_content = [block.model_dump(mode="json") for block in content]
    return len(json.dumps(wire_content, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _scoped_message_key(request: Request, agent_id: int, key: str) -> str:
    """Delivery and reconciliation identify the same logical POST operation."""
    from gateway.request_principal import PrincipalScopeError, request_key

    try:
        return request_key(request, key, method="POST", path=f"/api/agents/{agent_id}/messages")
    except PrincipalScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/agents/{agent_id}/messages", status_code=201)
async def post_agent_message(
    agent_id: int,
    body: AgentMessageIn,
    request: Request,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=128
    ),
) -> AgentMessageEnqueued:
    """Deliver a chat inbound to the specified agent.

    `content` is either a plain string or a list of OpenAI-shaped content blocks
    (multimodal: text + `image_url` referencing an upload of this agent). A
    string is a pure INSERT + return; a block list is capability-gated (422 if
    the agent's model cannot see images) and stored with the blocks in the
    inbound's JSONB payload for the claim node to inline natively.

    Auto-resurrect in `deliver_chat_inbound` ensures the message always reaches
    a live agent — the caller does not branch on status. An optional
    `Idempotency-Key` is stored transactionally with the inbound; same-key
    retries return the same `inbound_id` and mismatched key reuse fails 409.

    404: agent_id does not exist (AgentNotFound -> handler returns 404 + reason).
    409: the key already identifies a different agent/body/source.
    413: message content exceeds the 1 MiB transport limit.
    422: a block list gated out (non-vision model) or referencing a bad upload.
    """
    if idempotency_key is not None:
        idempotency_key = _scoped_message_key(request, agent_id, idempotency_key)
    if _message_content_size_bytes(body.content) > _MAX_MESSAGE_CONTENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                "message content exceeds the 1 MiB limit; write large content "
                "to a file and send the file path instead"
            ),
        )
    # AgentNotFound is returned 404 + reason by the handler; first SELECT
    # to verify existence then INSERT (same pattern as frontend
    # `/agents/{id}/messages`). After INSERT, SELECT once more to get the
    # true "delivery time" status — see docstring. Off the event loop:
    # get_agent_status opens a fresh DB connection.
    await asyncio.to_thread(get_agent_status, agent_id)
    if idempotency_key is not None:
        # Resolve a committed receipt before consulting mutable delivery
        # prerequisites. The original upload can disappear or the agent's model
        # can change after COMMIT; neither invalidates the durable identity. A
        # missing receipt still takes the normal validation + INSERT path, with
        # the unique index closing a concurrent first-delivery race.
        normalized_text, normalized_payload = _normalize_message_content(body.content)
        try:
            existing = await reconcile_chat_delivery(
                request.app.state.db_pool,
                agent_id,
                client_message_id=idempotency_key,
                content=normalized_text,
                source=body.source,
                payload=normalized_payload,
            )
        except ClientMessageConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if existing is not None:
            return AgentMessageEnqueued(
                status=existing.status,
                inbound_id=existing.inbound_id,
            )
    text, payload = _prepare_message_content(request, agent_id, body.content)
    try:
        delivery = await deliver_chat_inbound(
            request.app.state.db_pool,
            agent_id,
            prepare=lambda _conn: text,
            source=body.source,
            payload=payload,
            client_message_id=idempotency_key,
        )
    except ClientMessageConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AgentMessageEnqueued(status=delivery.status, inbound_id=delivery.inbound_id)


def _system_note_blocking(
    pool: ConnectionPool,
    agent_id: int,
    content: str,
    source: str,
    note_tag: str,
    task_id: int | None,
) -> int:
    """Sync system-note INSERT — via to_thread (pool work off the event loop)."""
    with write_transaction(pool) as conn:
        if task_id is not None:
            with conn.cursor() as cur:
                # Keep ownership stable until insert_inbound_message() commits below:
                # a reassignment must not land between attribution validation and
                # queueing the task-tagged note.
                cur.execute("SELECT owner FROM agent_tasks WHERE id = %s FOR UPDATE", (task_id,))
                row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=422, detail=f"task_id {task_id} does not exist")
            if row[0] != agent_id:
                raise HTTPException(
                    status_code=422,
                    detail=f"task_id {task_id} is not owned by agent {agent_id}",
                )
        return insert_inbound_message(
            conn,
            agent_id,
            content=content,
            source=source,
            kind=InboundKind.SYSTEM_NOTE.value,
            payload={"note_tag": note_tag, **({"task_id": task_id} if task_id is not None else {})},
        )


@router.post("/api/agents/{agent_id}/system-note", status_code=201)
async def post_agent_system_note(
    agent_id: int,
    body: SystemNoteIn,
    request: Request,
) -> AgentMessageEnqueued:
    """Deliver a framework system note to the specified agent.

    The note is queued as a kind='system_note' inbound; the agent's claim
    renders it as a system note (system_marker, NoteTag `body.note_tag`) in
    its timeline — no Agent prefix, no peer timestamp — instead of a chat
    peer message. Task notifications (assign / update / reminder) are the
    current family (`note_tag='task'`).

    `body.resurrect` mirrors the chat path's auto-resurrect but is a choice,
    not a given: task assignment (a delegator direction) wakes a terminated
    owner, while plain update / reminder notices must not (user ruling
    2026-08-27 — notification messages never resurrect a terminated owner).

    404: agent_id does not exist. 413: content exceeds the 1 MiB transport
    limit. 422: note_tag is not a NoteTag value, or source is not a legal
    envelope source.
    """
    if len(body.content.encode("utf-8")) > _MAX_MESSAGE_CONTENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                "system note content exceeds the 1 MiB limit; write large content "
                "to a file and send the file path instead"
            ),
        )
    await asyncio.to_thread(get_agent_status, agent_id)
    inbound_id = await asyncio.to_thread(
        _system_note_blocking,
        request.app.state.db_pool,
        agent_id,
        body.content,
        body.source,
        body.note_tag,
        body.task_id,
    )
    # Announce for the live UI (frontend badge / turn active), like chat.
    await _ops.publish_inbound_arrived(
        agent_id, inbound_id, InboundKind.SYSTEM_NOTE.value, body.source, body.content
    )
    if body.resurrect:
        status = await _ops.resurrect_if_terminated(
            agent_id,
            trigger_inbound_id=inbound_id,
            trigger_inbound_kind=InboundKind.SYSTEM_NOTE.value,
        )
    else:
        status = await asyncio.to_thread(get_agent_status, agent_id)
    return AgentMessageEnqueued(status=status, inbound_id=inbound_id)


@router.post("/api/agents/{agent_id}/messages/reconcile")
async def reconcile_agent_message(
    agent_id: int,
    body: AgentMessageIn,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
) -> AgentMessageEnqueued:
    """Resolve a timed-out POST and heal its still-pending delivery tail."""
    idempotency_key = _scoped_message_key(request, agent_id, idempotency_key)
    if _message_content_size_bytes(body.content) > _MAX_MESSAGE_CONTENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                "message content exceeds the 1 MiB limit; write large content "
                "to a file and send the file path instead"
            ),
        )
    await asyncio.to_thread(get_agent_status, agent_id)
    text, payload = _normalize_message_content(body.content)
    try:
        delivery = await reconcile_chat_delivery(
            request.app.state.db_pool,
            agent_id,
            client_message_id=idempotency_key,
            content=text,
            source=body.source,
            payload=payload,
        )
    except ClientMessageConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if delivery is None:
        raise HTTPException(status_code=404, detail="client message not committed")
    return AgentMessageEnqueued(status=delivery.status, inbound_id=delivery.inbound_id)


@router.get("/api/agents/{agent_id}/messages", dependencies=[Depends(deny_isolated_result_read)])
def get_agent_messages(
    agent_id: int,
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=10000),
    before: int | None = Query(default=None, ge=0),
) -> AgentMessagesResponse:
    """Raw state.messages for one agent — the data layer beneath the
    rendered GET .../timeline view. Consumers (ops scripts / other agents /
    evals) get each message as a LangChain BaseMessage `model_dump()`
    (type / content / tool_calls / id / additional_kwargs / ...) rather than
    the per-block TimelineItem rendering.

    Same path as `POST /api/agents/{id}/messages` but different method: the
    POST enqueues a new chat inbound for delivery; this GET reads back the
    full conversation history.

    Windowing is an absolute-integer-index analog of the timeline's tail-window
    mode (the timeline cursor is an `item_id` string + `has_more`; here the
    cursor is an absolute index into `state.messages`). No `limit` returns the
    newest 100 messages; `before=<index>` without a limit returns the newest
    100 messages before that exclusive cursor. An explicit `limit` (1..10000)
    preserves the requested page size. `messages[i]` corresponds to
    `state.messages[start_index + i]`; `msg_count` is the total length, and
    `has_more` tells the caller whether `start_index` can be supplied as the
    next `before` cursor for an older page.

    404: agent_id does not exist (same precondition as the timeline GET).
    503: the checkpoint store could not be read — a programmatic data endpoint
        does not disguise a read failure as an empty history; the caller retries
        or checks store health.
    """
    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    try:
        messages = load_checkpoint_messages(agent_id)
    except CheckpointReadError as exc:
        _log.warning("messages endpoint: checkpoint read failed for agent %s: %r", agent_id, exc)
        raise HTTPException(
            status_code=503, detail="checkpoint read failed; retry or check store health"
        ) from exc
    msg_count = len(messages)
    # `before` is an exclusive upper bound (absolute index); clamp to the
    # available range so an out-of-range cursor still returns a valid window.
    end = msg_count if before is None else min(before, msg_count)
    effective_limit = limit if limit is not None else _DEFAULT_MESSAGE_LIMIT
    start = max(0, end - effective_limit)
    window = messages[start:end]
    return AgentMessagesResponse(
        messages=[msg.model_dump() for msg in window],
        msg_count=msg_count,
        start_index=start,
        has_more=start > 0,
    )


@router.get(
    "/api/agents/{agent_id}/traces/{trace_id}/messages",
    dependencies=[Depends(deny_isolated_result_read)],
)
def get_trace_checkpoint_messages(
    agent_id: int,
    trace_id: str,
    request: Request,
) -> TraceCheckpointMessagesResponse:
    """Full turn content for one OTel trace, resolved on demand from checkpoints.

    Trace v2 content-retrieval path: spans carry metadata only (task #792), so
    clicking an LM span needs this endpoint to bring back the turn's complete
    messages — system prompt included (the checkpoint's `messages` channel is
    the whole conversation, not a window).

    `pruned` semantics: the checkpoint link is written by the agent after each
    turn (`checkpoints.metadata.trace_id`), and checkpoint_cleanup keeps only
    the latest K checkpoints per thread, so an old trace resolves to nothing —
    the response is pruned=true with an empty list and the caller renders the
    "trimmed" state. A trace id with no matching checkpoint is the same shape (the
    link itself may predate the feature or the turn committed no checkpoint).

    404: agent_id does not exist. 503: checkpoint store read failed.
    """
    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    try:
        checkpoint_id, messages = load_checkpoint_messages_by_trace(agent_id, trace_id)
    except CheckpointReadError as exc:
        _log.warning(
            "trace messages endpoint: checkpoint read failed for agent %s trace %s: %r",
            agent_id,
            trace_id,
            exc,
        )
        raise HTTPException(
            status_code=503, detail="checkpoint read failed; retry or check store health"
        ) from exc
    return TraceCheckpointMessagesResponse(
        trace_id=trace_id,
        agent_id=agent_id,
        checkpoint_id=checkpoint_id,
        pruned=checkpoint_id is None,
        messages=[msg.model_dump() for msg in messages],
    )


@router.get(
    "/api/agents/{agent_id}/last-message",
    dependencies=[Depends(deny_isolated_result_read)],
)
def get_last_message(
    agent_id: int,
    request: Request,
    caller: str = Query(..., description="Caller identifier, e.g. 'agent:240'"),
) -> LastMessageResponse:
    """Return the text of the last AI message for an agent.

    A non-isolated agent in the same cluster can query any other agent; an
    eval-isolated caller is denied before this result can become a replay leak.

    Returns ``text=None`` when the agent has no AI message with text
    content yet (no checkpoint / no AIMessage / content is not a string).

    Reads from ``agents_meta.last_message_text`` first — a column that
    survives compact (which replaces the entire checkpoint). Falls back
    to scanning the checkpoint when the column is NULL (backward compat
    with agents that have not yet written to it).
    """
    # Keep the documented, required marker contract. The shared dependency
    # reads the raw query parameter so every guarded route uses one policy.
    del caller
    # --- agent existence check + last_message_text read ---
    # Read last_message_text first as an optimization — it survives compact.
    # When the column is missing (migration not applied), fall through to the
    # checkpoint scan below rather than returning 500.
    last_message_text: str | None = None
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM agents_meta WHERE id = %s", (agent_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    try:
        with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT last_message_text FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
            if row and row[0]:
                last_message_text = row[0]
    except Exception:
        _log.warning(
            "last-message: last_message_text column read failed for agent %s, "
            "falling back to checkpoint scan",
            agent_id,
            exc_info=True,
        )
    if last_message_text:
        return LastMessageResponse(text=last_message_text)
    # --- fall back to checkpoint scan (backward compat) ---
    try:
        messages = load_checkpoint_messages(agent_id)
    except CheckpointReadError as exc:
        _log.warning("last-message: checkpoint read failed for agent %s: %r", agent_id, exc)
        raise HTTPException(
            status_code=503, detail="checkpoint read failed; retry or check store health"
        ) from exc
    for msg in reversed(messages):
        text = _extract_message_text(msg)
        if text is not None:
            return LastMessageResponse(text=text)
    return LastMessageResponse(text=None)


def _extract_message_text(msg: "AIMessage") -> str | None:  # noqa: F821, UP037  # pyright: ignore[reportUndefinedVariable]
    """Extract the text content from an AIMessage.

    AIMessage.content can be a plain string or a list of content blocks
    (e.g. ``[{"type": "text", "text": "hello"}]``). For list content,
    concatenate text-type blocks. Returns None when there is no text.
    """
    from langchain_core.messages import AIMessage

    if not isinstance(msg, AIMessage):
        return None
    content: Any = msg.content  # pyright: ignore[reportUnknownMemberType]
    if isinstance(content, str):
        return content if content else None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content_blocks(cast(list[Any], content)):
            if isinstance(block, dict) and cast(dict[str, Any], block).get("type") == "text":
                text = cast(dict[str, Any], block).get("text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts) if parts else None
    return None


@router.get("/api/agents/{agent_id}/pending", dependencies=[Depends(deny_isolated_result_read)])
def get_pending_messages(agent_id: int, request: Request) -> list[PendingInbound]:
    """Chat inbounds still queued for the agent (status='pending'), oldest
    first. These have not been claimed yet, so they are absent from the
    timeline snapshot; the web UI shows them as a compact strip above the
    composer. Once a message is claimed it enters the agent's messages and
    appears in the timeline, dropping out of this list.

    Returns an empty list for a nonexistent agent (this is a plain
    inbound-table read with no agent-existence precondition; a missing agent
    just has no pending rows).
    """
    with request.app.state.db_pool.connection() as conn:
        rows = list_pending_inbounds(conn, agent_id)
    return [
        PendingInbound(id=r.id, source=r.source, content=r.content, created_at=r.created_at)
        for r in rows
    ]


@router.get("/api/agents/{agent_id}/activity", dependencies=[Depends(deny_isolated_result_read)])
def get_activity_trail(agent_id: int, request: Request) -> list[agent_snapshot.ActivityEntry]:
    """The agent's activity trail, oldest first (historical rows; the SDK write
    verb `ava.self.log` was removed 2026-08-02, so new rows no longer appear).
    The collapsed current line is already on the agent snapshot
    (GET /api/agents); this endpoint backs the fleet view's replay of how the
    work progressed.

    Returns an empty list for an agent that has never reported or does not
    exist (a plain activity-table read with no agent-existence precondition).
    """
    with request.app.state.db_pool.connection() as conn:
        return agent_snapshot.select_activity_trail(conn, agent_id)


@router.get("/api/agents/{agent_id}/token-usage")
def get_token_usage(agent_id: int, request: Request) -> TokenUsageResponse:
    """The agent's most recent LLM call token usage. `input_tokens` is
    context-window occupancy; `max_input_tokens` is the model's context
    window ceiling so the frontend can render "context: 26k/1M tokens".
    When the frontend switches to an existing agent, it uses this endpoint
    to fetch the historical latest value to initialize the token counter
    UI — the SSE `token_usage` event is fire-and-forget without persistence,
    and the previous value is not available without switching to.

    Returns (0, 0, 0) on a checkpoint read failure — the intentional
    fail-fast boundary: the read failure is caught here (the SSE
    token_usage push refreshes the UI later), but the reverse scan over
    the loaded messages runs unguarded so a bug surfaces normally rather
    than being masked as 0/0.
    """
    from langchain_core.messages import AIMessage

    from shared.lm.context_budget import UnknownModelWindowError, resolve_context_budget
    from shared.lm.factory import ensure_provider_plugins_loaded

    # Plugin models must be registered before the registry lookup below.
    ensure_provider_plugins_loaded()

    # Look up the agent's model from config_overlay (per-agent override) else the
    # cluster default, and resolve its context budget: the window ceiling plus
    # the soft/hard compact thresholds (a per-agent fraction of the window).
    # An unknown model degrades all three to 0 — a display endpoint must not
    # 500 over a model-registry gap; the frontend hides an absent ceiling.
    max_input_tokens = soft_compact_tokens = hard_compact_tokens = 0
    try:
        with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT config_overlay FROM agents_meta WHERE id = %s",
                (agent_id,),
            )
            row = cur.fetchone()
        config_overlay: dict[str, Any] | None = row[0] if row and row[0] else None
        model: str | None = config_overlay.get("llm_model") if config_overlay else None
        if not model:
            from shared.config import settings

            model = settings.lm.llm_model
        if model:
            budget = resolve_context_budget(model)
            max_input_tokens = budget.max_context_tokens
            soft_compact_tokens = budget.soft_compact_tokens
            hard_compact_tokens = budget.hard_compact_tokens
    except UnknownModelWindowError as exc:
        _log.warning("token-usage: %s", exc)
    except Exception as exc:
        _log.warning("token-usage: config_overlay read failed for agent %s: %r", agent_id, exc)

    try:
        messages = load_checkpoint_messages(agent_id)
    except CheckpointReadError as exc:
        _log.warning(
            "token-usage: checkpoint read failed for agent %s, returning 0/0/0: %r",
            agent_id,
            exc,
        )
        return TokenUsageResponse(
            input_tokens=0,
            output_tokens=0,
            max_input_tokens=max_input_tokens,
            soft_compact_tokens=soft_compact_tokens,
            hard_compact_tokens=hard_compact_tokens,
        )
    # Scan in reverse for the most recent AIMessage with usage_metadata —
    # after the LLM call completes, llm_node appends final_msg to state, and
    # usage_metadata travels with it.
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            usage = msg.usage_metadata
            if usage:
                from shared.lm.reasoning import extract_reasoning_tokens

                return TokenUsageResponse(
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    reasoning_tokens=extract_reasoning_tokens(
                        usage, content=getattr(msg, "content", None)
                    ),
                    max_input_tokens=max_input_tokens,
                    soft_compact_tokens=soft_compact_tokens,
                    hard_compact_tokens=hard_compact_tokens,
                )
    return TokenUsageResponse(
        input_tokens=0,
        output_tokens=0,
        max_input_tokens=max_input_tokens,
        soft_compact_tokens=soft_compact_tokens,
        hard_compact_tokens=hard_compact_tokens,
    )


@router.get("/api/agents/{agent_id}/context-breakdown")
def get_context_breakdown(agent_id: int, request: Request) -> ContextBreakdownResponse:
    """How the agent's context window is spent, for the composer's breakdown
    panel (lazy-loaded on expand).

    Buckets the checkpoint messages by kind + splits the system prompt into its
    top-level sections, each a chars/4 estimate proportionally normalized to the
    last LLM call's real `input_tokens` (so the categories sum to the truth). Pure
    gateway-side view logic (`gateway/context_breakdown.py`) — one checkpoint read,
    no kernel/agent involvement. A checkpoint read failure / no checkpoint yields
    an empty breakdown with zeroed totals (same tolerance as token-usage: the
    panel re-opens fine later)."""
    from gateway.context_breakdown import SectionNode, compute_breakdown
    from shared.lm.context_budget import (
        UnknownModelWindowError,
        latest_input_tokens,
        resolve_context_budget,
    )
    from shared.lm.factory import ensure_provider_plugins_loaded

    # Plugin models must be registered before the registry lookup below.
    ensure_provider_plugins_loaded()

    def _to_context_section(node: SectionNode) -> ContextSection:
        return ContextSection(
            name=node.name,
            tokens=node.tokens,
            children=[_to_context_section(child) for child in node.children],
        )

    model = _resolve_agent_model(request, agent_id)
    max_input_tokens = soft_compact_tokens = hard_compact_tokens = 0
    try:
        budget = resolve_context_budget(model)
        max_input_tokens = budget.max_context_tokens
        soft_compact_tokens = budget.soft_compact_tokens
        hard_compact_tokens = budget.hard_compact_tokens
    except UnknownModelWindowError as exc:
        _log.warning("context-breakdown: %s", exc)

    try:
        messages = load_checkpoint_messages(agent_id)
    except CheckpointReadError as exc:
        _log.warning(
            "context-breakdown: checkpoint read failed for agent %s, returning empty: %r",
            agent_id,
            exc,
        )
        messages = []

    total_input_tokens = latest_input_tokens(messages) or 0
    categories, sections, estimated_total = compute_breakdown(messages, total_input_tokens)
    return ContextBreakdownResponse(
        total_input_tokens=total_input_tokens,
        estimated_total=estimated_total,
        max_input_tokens=max_input_tokens,
        soft_compact_tokens=soft_compact_tokens,
        hard_compact_tokens=hard_compact_tokens,
        sections=[_to_context_section(node) for node in sections],
        categories=[ContextCategory(kind=kind, tokens=tokens) for kind, tokens in categories],
    )
