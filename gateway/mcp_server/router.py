"""Human-authenticated administration for gateway MCP client credentials.

An MCP client token lives in `mcp_clients`, bound to no write generation, so
only the human secret (its bearer or a session it minted) manages them: a
machine token or a runner-minted session gets 403 (`require_human_credential`).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from gateway.http.auth.request_principal import require_human_credential
from gateway.mcp_server import clients
from gateway.mcp_server.clients import McpClientScope as McpClientScope
from gateway.mcp_server.creation_receipts import (
    CredentialCreationRequest,
    CredentialCreationResult,
    OperationKey,
    accept_creation,
)

router = APIRouter(dependencies=[Depends(require_human_credential)])


class McpClientCreate(BaseModel):
    name: str
    scope: McpClientScope = McpClientScope.READ


class McpClientCreated(BaseModel):
    id: int
    name: str
    scope: McpClientScope
    token: str


class McpClientView(BaseModel):
    id: int
    name: str
    scope: McpClientScope
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None


class McpClientRevoked(BaseModel):
    ok: bool


@router.get("/api/mcp/clients")
def get_mcp_clients(request: Request) -> list[McpClientView]:
    """List MCP clients without their credential hashes."""
    return [McpClientView(**row) for row in clients.list_clients(request.app.state.db_pool)]


@router.post("/api/mcp/clients")
def post_mcp_client(request: Request, body: McpClientCreate) -> McpClientCreated:
    """Create an MCP client and reveal its plaintext token once."""
    try:
        client_id, token = clients.create_client(request.app.state.db_pool, body.name, body.scope)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return McpClientCreated(id=client_id, name=body.name, scope=body.scope, token=token)


@router.post("/api/mcp/clients/{client_id}/revoke")
def post_mcp_client_revoke(request: Request, client_id: int) -> McpClientRevoked:
    """Revoke one active MCP client."""
    if not clients.revoke_client(request.app.state.db_pool, client_id):
        raise HTTPException(status_code=404, detail="active MCP client not found")
    return McpClientRevoked(ok=True)


@router.post("/api/keyed/v1/mcp/clients")
def post_guarded_mcp_client(
    request: Request, body: CredentialCreationRequest, operation_key: OperationKey
) -> CredentialCreationResult:
    """Create once; replay only original metadata, never the one-time plaintext token."""
    return accept_creation(request.app.state.db_pool, operation_key, body)
