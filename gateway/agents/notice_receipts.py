"""Notice operation receipts owned by the notice transaction, never a response cache."""

from collections.abc import Callable
from datetime import UTC, datetime

import psycopg
from fastapi import HTTPException, Request
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents.messages.inbound_provenance import InboundProvenance
from base.db import insert_inbound_message_in_transaction
from base.db.transaction import write_transaction
from gateway.agents.schemas import NoticeCreateIn
from gateway.auth.request_principal import PrincipalScopeError, request_key


def notice_key(request: Request, key: str | None) -> str | None:
    """Use the same credential scope as other gateway keyed operations."""
    if not isinstance(key, str):
        return None
    try:
        return request_key(request, key, method="POST", path=request.url.path)
    except PrincipalScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def existing_receipt(
    conn: psycopg.Connection,
    path: str,
    key: str,
    body: dict[str, object],
) -> dict[str, object] | None:
    """Serialize a notice operation and reject reuse with changed inputs."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (path + ":" + key,))
        cur.execute(
            "SELECT request, receipt FROM notice_operation_receipts "
            "WHERE path = %s AND operation_key = %s",
            (path, key),
        )
        row = cur.fetchone()
    if row is None:
        return None
    if row[0] != body:
        raise HTTPException(
            status_code=409, detail="idempotency key identifies a different notice operation"
        )
    return row[1]


def save_receipt(
    conn: psycopg.Connection,
    path: str,
    key: str,
    body: dict[str, object],
    receipt: dict[str, object],
) -> None:
    """Persist an immutable result in the transaction containing its effects."""
    conn.execute(
        "INSERT INTO notice_operation_receipts (path, operation_key, request, receipt) "
        "VALUES (%s, %s, %s, %s)",
        (path, key, Jsonb(body), Jsonb(receipt)),
    )


def resolve_once(
    pool: ConnectionPool,
    path: str,
    key: str,
    body: dict[str, object],
    agent_id: int,
    source: str,
    prepare: Callable[[psycopg.Connection], str | None],
    provenance: InboundProvenance,
) -> dict[str, object]:
    """Resolve and insert the reply with one durable operation receipt."""
    with write_transaction(pool) as conn:
        previous = existing_receipt(conn, path, key, body)
        if previous is not None:
            return previous
        content = prepare(conn)
        inbound_id = None
        if content is not None:
            with conn.cursor() as cur:
                inbound_id, _ = insert_inbound_message_in_transaction(
                    cur,
                    agent_id,
                    content,
                    source,
                    provenance=provenance,
                )
        receipt: dict[str, object] = {"inbound_id": inbound_id, "content": content}
        save_receipt(conn, path, key, body, receipt)
    return receipt


def validate_creation(body: NoticeCreateIn) -> None:
    """Reject invalid notice obligations before allocating a receipt."""
    if not body.title.strip():
        raise HTTPException(status_code=422, detail="title must be non-empty")
    if body.blocking and not body.require_response:
        raise HTTPException(
            status_code=422,
            detail="blocking=True requires require_response=True (an FYI never stalls)",
        )


def validate_creation_state(conn: psycopg.Connection, body: NoticeCreateIn) -> None:
    """Validate mutable creation preconditions only for a fresh operation."""
    if body.task_id is not None:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM agent_tasks WHERE id = %s", (body.task_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=422, detail=f"task {body.task_id} does not exist")
    if body.expire_at is not None:
        expiry = (
            body.expire_at
            if body.expire_at.tzinfo is not None
            else body.expire_at.replace(tzinfo=UTC)
        )
        if expiry < datetime.now(UTC):
            raise HTTPException(
                status_code=422, detail=f"expire_at is in the past: {expiry.isoformat()}"
            )
