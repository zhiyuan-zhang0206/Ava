"""Cluster-authenticated administration for gateway MCP client credentials."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from gateway import mcp_clients

router = APIRouter()

McpClientScope = Literal["read", "write"]


class McpClientCreate(BaseModel):
    name: str
    scope: McpClientScope = "read"


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
    return [McpClientView(**row) for row in mcp_clients.list_clients(request.app.state.db_pool)]


@router.post("/api/mcp/clients")
def post_mcp_client(request: Request, body: McpClientCreate) -> McpClientCreated:
    """Create an MCP client and reveal its plaintext token once."""
    try:
        client_id, token = mcp_clients.create_client(
            request.app.state.db_pool, body.name, body.scope
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return McpClientCreated(id=client_id, name=body.name, scope=body.scope, token=token)


@router.post("/api/mcp/clients/{client_id}/revoke")
def post_mcp_client_revoke(request: Request, client_id: int) -> McpClientRevoked:
    """Revoke one active MCP client."""
    if not mcp_clients.revoke_client(request.app.state.db_pool, client_id):
        raise HTTPException(status_code=404, detail="active MCP client not found")
    return McpClientRevoked(ok=True)
