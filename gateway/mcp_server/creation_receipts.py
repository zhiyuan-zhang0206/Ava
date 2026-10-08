"""Retained MCP credential creation metadata; plaintext tokens are never replayed."""

import hashlib
import json
from datetime import datetime
from typing import Annotated

import psycopg
from fastapi import Depends, Header, HTTPException, Request
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from base.db.transaction import write_transaction
from gateway.http.auth.request_principal import (
    PRINCIPAL_SCOPE,
    SCOPE_HEADER,
    AuthPrincipal,
    PrincipalScopeError,
    principal_key,
)
from gateway.mcp_server.clients import McpClientScope, create_client_in_transaction


class CredentialCreationRequest(BaseModel):
    """One credential creation intent; its caller key is supplied separately."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128, strict=True)
    scope: McpClientScope = McpClientScope.READ


class CredentialCreationResult(BaseModel):
    """Original creation metadata; replay deliberately cannot recover a token."""

    id: int
    name: str
    scope: McpClientScope
    created_at: datetime
    replayed: bool
    token: str | None


def operation_key(
    request: Request,
    key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    scope: str = Header(alias=SCOPE_HEADER),
) -> str:
    """Require the authenticated principal namespace before any credential effect."""
    principal = getattr(request.state, "auth_principal", None)
    if scope != PRINCIPAL_SCOPE or not isinstance(principal, AuthPrincipal):
        raise HTTPException(
            status_code=422, detail="guarded credentials require principal-v1 scope"
        )
    try:
        return principal_key(principal, request.method, request.url.path, key)
    except (PrincipalScopeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


OperationKey = Annotated[str, Depends(operation_key)]


def accept_creation(
    pool: ConnectionPool, key: str, body: CredentialCreationRequest
) -> CredentialCreationResult:
    """Commit the credential and token-free metadata receipt in one transaction."""
    digest = hashlib.sha256(
        json.dumps(body.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    try:
        with write_transaction(pool) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))
            prior = conn.execute(
                "SELECT request_hash, acceptance FROM mcp_credential_creation_receipts "
                "WHERE operation_key=%s",
                (key,),
            ).fetchone()
            if prior is not None:
                if prior[0] != digest:
                    raise HTTPException(status_code=409, detail="credential intent changed")
                return CredentialCreationResult(**prior[1], replayed=True, token=None)
            metadata, token = create_client_in_transaction(conn, body.name, body.scope)
            metadata["created_at"] = metadata["created_at"].isoformat()
            conn.execute(
                "INSERT INTO mcp_credential_creation_receipts "
                "(operation_key,request_hash,acceptance) VALUES (%s,%s,%s)",
                (key, digest, Jsonb(metadata)),
            )
            result = CredentialCreationResult(**metadata, replayed=False, token=token)
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="MCP client name already exists") from exc
    return result
