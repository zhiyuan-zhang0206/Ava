"""Principal-bound page acceptance snapshots, owned by the registry transaction."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request
from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents import AgentStatus
from base.db.transaction import write_transaction
from gateway.http.auth.request_principal import (
    PRINCIPAL_SCOPE,
    SCOPE_HEADER,
    AuthPrincipal,
    PrincipalScopeError,
    principal_key,
)
from gateway.schemas.pages import PageRegisterRequest
from ops.pages import (
    PagePortConflictError,
    PageTargetChangedError,
    assert_port_free,
    close_all_agent_pages_in_transaction,
    close_observed_page_in_transaction,
    lock_page_agent,
    register_page_in_transaction,
)
from ops.rpc_schemas import PageRow


@dataclass(frozen=True)
class PageAcceptance:
    record: PageRow
    closed_names: list[str]
    replayed: bool


def operation_key(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    idempotency_scope: str = Header(alias=SCOPE_HEADER),
) -> str:
    """Require the strong entry's verified, explicitly scoped operation identity."""
    principal = getattr(request.state, "auth_principal", None)
    if idempotency_scope != PRINCIPAL_SCOPE or not isinstance(principal, AuthPrincipal):
        raise HTTPException(
            status_code=422, detail="guarded pages require verified principal-v1 scope"
        )
    try:
        return principal_key(principal, request.method, request.url.path, idempotency_key)
    except (PrincipalScopeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _begin(conn: Connection, key: str, payload: dict[str, object]) -> tuple[str, PageRow | None]:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
    row = conn.execute(
        "SELECT request_hash, acceptance FROM page_operation_receipts WHERE operation_key = %s",
        (key,),
    ).fetchone()
    if row is None:
        return digest, None
    if row[0] != digest:
        raise HTTPException(
            status_code=409, detail="idempotency key was already used for a different page request"
        )
    return digest, PageRow.model_validate(row[1])


def _finish(conn: Connection, key: str, digest: str, record: PageRow) -> None:
    conn.execute(
        "INSERT INTO page_operation_receipts (operation_key, request_hash, acceptance) VALUES (%s,%s,%s)",
        (key, digest, Jsonb(record.model_dump(mode="json"))),
    )


def _target(conn: Connection, agent_id: int, *, register: bool) -> None:
    lock_page_agent(conn, agent_id)
    exists = conn.execute(
        "SELECT id FROM agents WHERE id = %s FOR KEY SHARE", (agent_id,)
    ).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    row = conn.execute(
        "SELECT status FROM agents_meta WHERE id = %s FOR UPDATE", (agent_id,)
    ).fetchone()
    if register and row is not None and row[0] == AgentStatus.TERMINATED:
        raise HTTPException(
            status_code=409, detail=f"agent {agent_id} is terminated, cannot register page"
        )


def register(
    pool: ConnectionPool,
    key: str,
    agent_id: int,
    body: PageRegisterRequest,
    absolute_base: str,
    validate_target: Callable[[Connection], None],
) -> PageAcceptance:
    try:
        with write_transaction(pool) as conn:
            digest, replay = _begin(conn, key, body.model_dump(mode="json"))
            if replay is not None:
                return PageAcceptance(replay, [], replayed=True)
            _target(conn, agent_id, register=True)
            validate_target(conn)
            assert_port_free(conn, agent_id, body.host, body.port)
            closed = close_all_agent_pages_in_transaction(conn, agent_id, include_expired=True)
            record = register_page_in_transaction(
                conn,
                agent_id,
                body.name,
                body.port,
                body.host,
                body.title,
                body.serve_dir,
                body.ttl_seconds,
            )
            record = record.model_copy(update={"url": absolute_base + record.url})
            _finish(conn, key, digest, record)
            return PageAcceptance(record, closed, replayed=False)
    except PagePortConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def close(
    pool: ConnectionPool,
    key: str,
    agent_id: int,
    name: str,
    expected_page_id: int,
    absolute_base: str,
) -> PageAcceptance:
    try:
        with write_transaction(pool) as conn:
            digest, replay = _begin(conn, key, {"name": name, "expected_page_id": expected_page_id})
            if replay is not None:
                return PageAcceptance(replay, [], replayed=True)
            _target(conn, agent_id, register=False)
            record, changed = close_observed_page_in_transaction(
                conn, agent_id, name, expected_page_id
            )
            record = record.model_copy(update={"url": absolute_base + record.url})
            _finish(conn, key, digest, record)
            return PageAcceptance(record, [name] if changed else [], replayed=False)
    except PageTargetChangedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
