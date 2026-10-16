from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from langchain_core.messages import AIMessage, BaseMessage
from psycopg_pool import ConnectionPool

from base.agents.history.checkpoint import (
    CheckpointReadError,
    load_checkpoint_messages,
    load_checkpoint_messages_by_trace,
)
from base.agents.history.context_breakdown import latest_request_breakdown
from base.agents.history.context_response import (
    ContextBreakdownResponse,
    context_breakdown_response,
    resolve_agent_model,
)
from base.agents.inbox.chat_delivery import ClientMessageConflictError
from base.agents.inbox.rows import list_pending_inbounds
from base.agents.messages.inbound import InboundKind
from base.agents.messages.inbound_images import image_mime_for, inbound_image_urls, parse_upload_url
from base.agents.model.model_overrides import agent_overrides
from base.agents.upload_delivery.paths import resolve_upload_path
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.daemon.schedules.completion_notices import (
    CompletionNotice,
    current_default_completion_notice_policy,
    delivery_required_for_agent,
)
from base.db import agent_exists
from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.agents.inbound_provenance import request_inbound_provenance
from gateway.agents.schemas import (
    AgentMessageEnqueued,
    AgentMessagesResponse,
    LastMessageResponse,
    PendingInbound,
    SystemNoteIn,
    TokenUsageResponse,
    TraceCheckpointMessagesResponse,
)
from gateway.agents.system_note import system_note_blocking as system_note_blocking
from gateway.http.model_catalog import require_model_catalog
from ops import lifecycle as _ops
from ops.agents import get_agent_status
from ops.lifecycle.delivery import deliver_chat_inbound, reconcile_chat_delivery
from ops.rpc_schemas import AgentMessageIn, ContentBlock, ImageUrlContentBlock, TextContentBlock

router = APIRouter()
_log = logging.getLogger(__name__)


_MAX_MESSAGE_CONTENT_BYTES = 1_048_576


def _validate_image_ref(agent_id: int, url: str) -> None:

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

    from base.lm.factory import model_supports_vision, vision_capable_provider_names

    if isinstance(content, str):
        return content, None

    blocks = content
    if any(isinstance(b, ImageUrlContentBlock) for b in blocks):
        catalog = require_model_catalog(request)
        model = resolve_agent_model(
            request.app.state.db_pool,
            agent_id,
            catalog=catalog,
            default_model_reader=lambda: cast(
                str, request.app.state.config_authority.service_field_value("llm_model")
            ),
        )
        if not model_supports_vision(model, catalog=catalog):
            raise HTTPException(
                422,
                f"agent {agent_id}'s model {model!r} cannot see images — "
                "switch it to a vision-capable model "
                f"({', '.join(vision_capable_provider_names(catalog=catalog))})",
            )
        for b in blocks:
            if isinstance(b, ImageUrlContentBlock):
                _validate_image_ref(agent_id, b.image_url.url)

    return _normalize_message_content(content)


def _normalize_message_content(
    content: str | list[ContentBlock],
) -> tuple[str, dict[str, object] | None]:

    if isinstance(content, str):
        return content, None
    text = "\n".join(
        block.text
        for block in content
        if isinstance(block, TextContentBlock) and block.text.strip()
    )

    text = text or "[image]"
    payload: dict[str, object] = {"content_blocks": [block.model_dump() for block in content]}
    return text, payload


def _message_content_size_bytes(content: str | list[ContentBlock]) -> int:
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    wire_content = [block.model_dump(mode="json") for block in content]
    return len(json.dumps(wire_content, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def scoped_message_key(request: Request, key: str) -> str:

    from base.api_contracts.idempotency import chat_message_key
    from gateway.http.auth.request_principal import PrincipalScopeError, keyed_principal

    try:
        keyed_principal(request, key)
        return chat_message_key(key)
    except (PrincipalScopeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _completion_delivery_required(
    agent_id: int,
    notice: CompletionNotice,
    pool: ConnectionPool,
    *,
    authority: ConfigAuthority,
) -> bool:

    with pool.connection() as conn:
        required = delivery_required_for_agent(
            conn,
            agent_id,
            notice,
            current_default_completion_notice_policy(authority),
        )
        conn.commit()
    return required


@router.post(
    "/api/agents/{agent_id}/messages",
    status_code=201,
    description="Deliver a chat inbound to the specified agent.\n\n`content` is either a plain string or a list of OpenAI-shaped content blocks\n(multimodal: text + `image_url` referencing an upload of this agent). A\nstring is a pure INSERT + return; a block list is capability-gated (422 if\nthe agent's model cannot see images) and stored with the blocks in the\ninbound's JSONB payload for the claim node to inline natively.\n\nAuto-resurrect in `deliver_chat_inbound` ensures the message always reaches\na live agent — the caller does not branch on status. An optional\n`Idempotency-Key` (always sent with `Idempotency-Scope: principal-v1`) is\nstored transactionally with the inbound; same-key retries return the same\n`inbound_id` and mismatched key reuse fails 409.\n\n404: agent_id does not exist (AgentNotFound -> handler returns 404 + reason).\n409: the key already identifies a different agent/body/source.\n413: message content exceeds the 1 MiB transport limit.\n422: a block list gated out (non-vision model) or referencing a bad upload,\nor a key without the principal-v1 scope header.",
)
async def post_agent_message(
    agent_id: int,
    body: AgentMessageIn,
    request: Request,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=128
    ),
) -> AgentMessageEnqueued:

    provenance = request_inbound_provenance(request)
    if idempotency_key is not None:
        idempotency_key = scoped_message_key(request, idempotency_key)
    if _message_content_size_bytes(body.content) > _MAX_MESSAGE_CONTENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                "message content exceeds the 1 MiB limit; write large content "
                "to a file and send the file path instead"
            ),
        )

    await asyncio.to_thread(get_agent_status, request.app.state.db, agent_id)
    if idempotency_key is not None:
        normalized_text, normalized_payload = _normalize_message_content(body.content)
        try:
            existing = await reconcile_chat_delivery(
                request.app.state.db_pool,
                request.app.state.db,
                request.app.state.bus,
                agent_id,
                client_message_id=idempotency_key,
                content=normalized_text,
                source=body.source,
                payload=normalized_payload,
                local_machine=request.app.state.machine.name(),
            )
        except ClientMessageConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if existing is not None:
            return AgentMessageEnqueued(status=existing.status, inbound_id=existing.inbound_id)
    if body.completion_notice:
        if not isinstance(body.content, str):
            raise RuntimeError("completion notice schema admitted non-string content")
        notice = CompletionNotice(source=body.source, content=body.content)
        if not await asyncio.to_thread(
            _completion_delivery_required,
            agent_id,
            notice,
            request.app.state.db_pool,
            authority=request.app.state.config_authority,
        ):
            return AgentMessageEnqueued(
                status=await asyncio.to_thread(get_agent_status, request.app.state.db, agent_id),
                inbound_id=None,
            )
    text, payload = _prepare_message_content(request, agent_id, body.content)
    try:
        delivery = await deliver_chat_inbound(
            request.app.state.db_pool,
            request.app.state.db,
            request.app.state.bus,
            agent_id,
            prepare=lambda _conn: text,
            source=body.source,
            payload=payload,
            client_message_id=idempotency_key,
            provenance=provenance,
            local_machine=request.app.state.machine.name(),
        )
    except ClientMessageConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AgentMessageEnqueued(status=delivery.status, inbound_id=delivery.inbound_id)


@router.post(
    "/api/agents/{agent_id}/system-note",
    status_code=201,
    description="Deliver a framework system note to the specified agent.\n\nThe note is queued as a kind='system_note' inbound; the agent's claim\nrenders it as a system note (system_marker, NoteTag `body.note_tag`) in\nits timeline — no Agent prefix, no peer timestamp — instead of a chat\npeer message. Task notifications (assign / update / reminder) are the\ncurrent family (`note_tag='task'`).\n\n`body.resurrect` mirrors the chat path's auto-resurrect but is a choice,\nnot a given: task assignment (a delegator direction) wakes a terminated\nowner, while plain update / reminder notices must not (user ruling\n2026-08-27 — notification messages never resurrect a terminated owner).\n\nA required Idempotency-Key names one logical note, including its\nresurrection policy. Replays return the original inbound id and repair\nits wake tail; changed requests conflict (409). The inbound remains kind='system_note'.\n\n404: agent_id does not exist. 413: content exceeds the 1 MiB transport\nlimit. 422: note_tag is not a NoteTag value, or source is not a legal\nenvelope source.",
)
async def post_agent_system_note(
    agent_id: int,
    body: SystemNoteIn,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
) -> AgentMessageEnqueued:

    if len(body.content.encode("utf-8")) > _MAX_MESSAGE_CONTENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                "system note content exceeds the 1 MiB limit; write large content "
                "to a file and send the file path instead"
            ),
        )
    from gateway.http.auth.request_principal import PrincipalScopeError, request_key

    try:
        idempotency_key = request_key(
            request,
            idempotency_key,
            method="POST",
            path=f"/api/agents/{agent_id}/system-note",
        )
    except PrincipalScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await asyncio.to_thread(get_agent_status, request.app.state.db, agent_id)
    inbound_id = await asyncio.to_thread(
        system_note_blocking,
        request.app.state.db,
        request.app.state.bus,
        request.app.state.db_pool,
        agent_id,
        body.content,
        body.source,
        body.note_tag,
        body.task_id,
        request_inbound_provenance(request),
        client_message_id=idempotency_key,
        resurrect=body.resurrect,
    )

    note_kind = InboundKind.SYSTEM_NOTE
    await _ops.publish_inbound_arrived(
        request.app.state.bus, agent_id, inbound_id, note_kind, body.source, body.content
    )
    if body.resurrect:
        status = await _ops.resurrect_if_terminated(
            request.app.state.db,
            request.app.state.bus,
            agent_id,
            trigger_inbound_id=inbound_id,
            trigger_inbound_kind=note_kind,
            local_machine=request.app.state.machine.name(),
        )
    else:
        status = await asyncio.to_thread(get_agent_status, request.app.state.db, agent_id)
    return AgentMessageEnqueued(status=status, inbound_id=inbound_id)


@router.post(
    "/api/agents/{agent_id}/messages/reconcile",
    description="Resolve a timed-out POST and heal its still-pending delivery tail.",
)
async def reconcile_agent_message(
    agent_id: int,
    body: AgentMessageIn,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
) -> AgentMessageEnqueued:

    idempotency_key = scoped_message_key(request, idempotency_key)
    if _message_content_size_bytes(body.content) > _MAX_MESSAGE_CONTENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                "message content exceeds the 1 MiB limit; write large content "
                "to a file and send the file path instead"
            ),
        )
    await asyncio.to_thread(get_agent_status, request.app.state.db, agent_id)
    text, payload = _normalize_message_content(body.content)
    try:
        delivery = await reconcile_chat_delivery(
            request.app.state.db_pool,
            request.app.state.db,
            request.app.state.bus,
            agent_id,
            client_message_id=idempotency_key,
            content=text,
            source=body.source,
            payload=payload,
            local_machine=request.app.state.machine.name(),
        )
    except ClientMessageConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if delivery is None:
        raise HTTPException(status_code=404, detail="client message not committed")
    return AgentMessageEnqueued(status=delivery.status, inbound_id=delivery.inbound_id)


@router.get(
    "/api/agents/{agent_id}/messages",
    dependencies=[Depends(deny_isolated_result_read)],
    description="Raw state.messages for one agent — the data layer beneath the\nrendered GET .../timeline view. Consumers (ops scripts / other agents /\nevals) get each message as a LangChain BaseMessage `model_dump()`\n(type / content / tool_calls / id / additional_kwargs / ...) rather than\nthe per-block TimelineItem rendering.\n\nSame path as `POST /api/agents/{id}/messages` but different method: the\nPOST enqueues a new chat inbound for delivery; this GET reads back the\nfull conversation history.\n\nWindowing is an absolute-integer-index analog of the timeline's tail-window\nmode (the timeline cursor is an `item_id` string + `has_more`; here the\ncursor is an absolute index into `state.messages`). No `limit` returns the\nconfigured default window (``display.messages_default_limit`` — 100 by\ndefault); `before=<index>` without a limit returns that window immediately\nbefore the exclusive cursor. An explicit `limit` (1..10000)\npreserves the requested page size. `messages[i]` corresponds to\n`state.messages[start_index + i]`; `msg_count` is the total length, and\n`has_more` tells the caller whether `start_index` can be supplied as the\nnext `before` cursor for an older page.\n\n404: agent_id does not exist (same precondition as the timeline GET).\n503: the checkpoint store could not be read — a programmatic data endpoint\n    does not disguise a read failure as an empty history; the caller retries\n    or checks store health.",
)
def get_agent_messages(
    agent_id: int,
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=10000),
    before: int | None = Query(default=None, ge=0),
) -> AgentMessagesResponse:

    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    try:
        messages = load_checkpoint_messages(request.app.state.db, agent_id)
    except CheckpointReadError as exc:
        _log.warning("messages endpoint: checkpoint read failed for agent %s: %r", agent_id, exc)
        raise HTTPException(
            status_code=503, detail="checkpoint read failed; retry or check store health"
        ) from exc
    msg_count = len(messages)

    end = msg_count if before is None else min(before, msg_count)
    effective_limit = limit if limit is not None else settings.display.messages_default_limit
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
    description="Full turn content for one OTel trace, resolved on demand from checkpoints.\n\nTrace v2 content-retrieval path: spans carry metadata only (task #792), so\nclicking an LM span needs this endpoint to bring back the turn's complete\nmessages — system prompt included (the checkpoint's `messages` channel is\nthe whole conversation, not a window).\n\n`pruned` semantics: the checkpoint link is written by the agent after each\nturn (`checkpoints.metadata.trace_id`). Checkpoints are never deleted, so\na trace id that resolves to nothing means no checkpoint carries it (the\nlink itself may predate the feature or the turn committed no checkpoint);\nthe response is then pruned=true with an empty list, which the caller\nrenders as absent content rather than an error.\n\n404: agent_id does not exist. 503: checkpoint store read failed.",
)
def get_trace_checkpoint_messages(
    agent_id: int, trace_id: str, request: Request
) -> TraceCheckpointMessagesResponse:

    db = request.app.state.db
    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    try:
        checkpoint_id, messages = load_checkpoint_messages_by_trace(db, agent_id, trace_id)
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
    description="Return the text of the last AI message for an agent.\n\nA non-isolated agent in the same cluster can query any other agent; an\neval-isolated caller is denied before this result can become a replay leak.\n\nReturns ``text=None`` when the agent has no AI message with text\ncontent yet.\n\nReads ``agents_meta.last_message_text`` — a column the agent process\nwrites after each LLM turn that produced text and that survives compact\n(which replaces the entire checkpoint), so this read never touches the\ncheckpoint.",
)
def get_last_message(
    agent_id: int,
    request: Request,
    caller: str = Query(..., description="Caller identifier, e.g. 'agent:240'"),
) -> LastMessageResponse:

    del caller
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT last_message_text FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    return LastMessageResponse(text=row[0] or None)


@router.get(
    "/api/agents/{agent_id}/pending",
    dependencies=[Depends(deny_isolated_result_read)],
    description="Chat inbounds still queued for the agent (status='pending'), oldest\nfirst. These have not been claimed yet, so they are absent from the\ntimeline snapshot; the web UI shows them as a compact strip above the\ncomposer. Once a message is claimed it enters the agent's messages and\nappears in the timeline, dropping out of this list. A chat the active takeover\nabsorbed (trail or relay read) is omitted too (#3683). A multimodal\nmessage carries its image reference urls (`images`) so the strip can\nrender thumbnails before the claim.\n\nReturns an empty list for a nonexistent agent (this is a plain\ninbound-table read with no agent-existence precondition; a missing agent\njust has no pending rows).",
)
def get_pending_messages(agent_id: int, request: Request) -> list[PendingInbound]:

    with request.app.state.db_pool.connection() as conn:
        rows = list_pending_inbounds(conn, agent_id)
    return [
        PendingInbound(
            id=r.id,
            source=r.source,
            content=r.content,
            images=inbound_image_urls(agent_id, r.payload),
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.get(
    "/api/agents/{agent_id}/token-usage",
    description="The agent's most recent LLM call token usage. `input_tokens` is\ncontext-window occupancy; `max_input_tokens` is the model's context\nwindow ceiling so the frontend can render \"context: 26k/1M tokens\".\nWhen the frontend switches to an existing agent, it uses this endpoint\nto fetch the historical latest value to initialize the token counter\nUI — the SSE `token_usage` event is fire-and-forget without persistence,\nand the previous value is not available without switching to.\n\nReturns (0, 0, 0) on a checkpoint read failure — the intentional\nfail-fast boundary: the read failure is caught here (the SSE\ntoken_usage push refreshes the UI later), but the reverse scan over\nthe loaded messages runs unguarded so a bug surfaces normally rather\nthan being masked as 0/0.",
)
def get_token_usage(agent_id: int, request: Request) -> TokenUsageResponse:

    try:
        messages = load_checkpoint_messages(request.app.state.db, agent_id)
    except CheckpointReadError as exc:
        _log.warning(
            "token-usage: checkpoint read failed for agent %s, returning 0/0/0: %r",
            agent_id,
            exc,
        )
        messages = []
    return token_usage_response(agent_id, request, messages)


def token_usage_response(
    agent_id: int, request: Request, messages: Sequence[BaseMessage]
) -> TokenUsageResponse:

    from base.lm.context_budget import UnknownModelWindowError, resolve_context_budget

    max_input_tokens = soft_compact_tokens = hard_compact_tokens = 0
    catalog = require_model_catalog(request)
    try:
        with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT config_overlay, birth_config FROM agents_meta WHERE id = %s",
                (agent_id,),
            )
            row = cur.fetchone()
        config_overlay: dict[str, Any] | None = row[0] if row and row[0] else None
        overrides = agent_overrides(config_overlay, row[1] if row else None)
        model: str | None = config_overlay.get("llm_model") if config_overlay else None
        if not model:
            from base.config import settings

            model = settings.lm.llm_model
        if model:
            budget = resolve_context_budget(model, catalog=catalog, overrides=overrides)
            max_input_tokens = budget.max_context_tokens
            soft_compact_tokens = budget.soft_compact_tokens
            hard_compact_tokens = budget.hard_compact_tokens
    except UnknownModelWindowError as exc:
        _log.warning("token-usage: %s", exc)
    except Exception as exc:
        _log.warning("token-usage: config_overlay read failed for agent %s: %r", agent_id, exc)

    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            usage = msg.usage_metadata
            if usage:
                from base.lm.reasoning import extract_reasoning_tokens

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


@router.get(
    "/api/agents/{agent_id}/context-breakdown",
    description="How the agent's context window is spent, for the composer's breakdown\npanel (lazy-loaded on expand).\n\nThe breakdown of the latest LLM request's input: each message's tokens anchored to the\nprovider's reported `input_tokens` (`base/agents/history/message_tokens.py`), buckets summed\nfrom them, only the inside of a message split by an estimator. Pure view logic\n(`base/agents/history/context_breakdown.py`) — one checkpoint read, no kernel/agent involvement.\nA checkpoint read failure / no checkpoint / no LLM request yet yields an empty breakdown\nwith zeroed totals (same tolerance as token-usage: the panel re-opens fine later).",
)
def get_context_breakdown(agent_id: int, request: Request) -> ContextBreakdownResponse:

    try:
        messages = load_checkpoint_messages(request.app.state.db, agent_id)
    except CheckpointReadError as exc:
        _log.warning(
            "context-breakdown: checkpoint read failed for agent %s, returning empty: %r",
            agent_id,
            exc,
        )
        messages = []
    return context_breakdown_response(
        request.app.state.db_pool,
        agent_id,
        latest_request_breakdown(messages),
        catalog=require_model_catalog(request),
        default_model_reader=lambda: cast(
            str, request.app.state.config_authority.service_field_value("llm_model")
        ),
    )
