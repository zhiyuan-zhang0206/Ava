"""Persistent, revocable credentials for callers of the gateway /mcp endpoint."""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from shared.db_transaction import write_transaction


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_client(pool: ConnectionPool[Any], name: str, scope: str) -> tuple[int, str]:
    """Create a client and return its id plus the plaintext token shown once."""
    token = secrets.token_urlsafe(32)
    try:
        with write_transaction(pool) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO mcp_clients (name, token_hash, scope) "
                "VALUES (%s, %s, %s) RETURNING id",
                (name, _token_hash(token), scope),
            )
            row = cur.fetchone()
            assert row is not None  # noqa: S101 — INSERT ... RETURNING always yields a row
    except psycopg.errors.UniqueViolation as exc:
        raise ValueError(f"MCP client named {name!r} already exists") from exc
    return int(row[0]), token


def lookup_client_by_token(pool: ConnectionPool[Any], token: str) -> dict[str, Any] | None:
    """Authenticate an active token and record its latest use atomically."""
    with write_transaction(pool) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            UPDATE mcp_clients
            SET last_used_at = now()
            WHERE token_hash = %s AND revoked_at IS NULL
            RETURNING id, name, scope, revoked_at
            """,
            (_token_hash(token),),
        )
        return cur.fetchone()


def list_clients(pool: ConnectionPool[Any]) -> list[dict[str, Any]]:
    """List client metadata without credential hashes."""
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, name, scope, created_at, revoked_at, last_used_at
            FROM mcp_clients
            ORDER BY id
            """
        )
        return cur.fetchall()


def revoke_client(pool: ConnectionPool[Any], client_id: int) -> bool:
    """Revoke an active client; return false when missing or already revoked."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mcp_clients
            SET revoked_at = now()
            WHERE id = %s AND revoked_at IS NULL
            RETURNING id
            """,
            (client_id,),
        )
        return cur.fetchone() is not None
