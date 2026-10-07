"""Schedule mutation identities committed with desired state and queued work."""

from datetime import datetime
from typing import Any

from fastapi import HTTPException, Request
from psycopg import Connection
from psycopg.types.json import Jsonb

from gateway.auth.request_principal import PrincipalScopeError, request_key


def operation_key(request: Request) -> str | None:
    key = request.headers.get("Idempotency-Key")
    if key is None:
        if request.headers.get("Idempotency-Scope") is not None:
            raise HTTPException(
                status_code=400, detail="Idempotency-Scope requires Idempotency-Key"
            )
        return None
    if not key or len(key) > 128:
        raise HTTPException(
            status_code=400, detail="idempotency key must contain 1 to 128 characters"
        )
    try:
        scoped = request_key(request, key, method=request.method, path=request.url.path)
    except PrincipalScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Legacy key space must still distinguish routes, unlike a principal-v1
    # key which already includes this identity. No caller-controlled labels.
    return f"{request.method}:{request.url.path}:{scoped}"


def begin(
    conn: Connection[Any], key: str | None, payload: dict[str, Any]
) -> tuple[Any, ...] | None:
    if key is None:
        return None
    conn.execute(
        "INSERT INTO schedule_operation_receipts (operation_key, request) VALUES (%s, %s) "
        "ON CONFLICT DO NOTHING",
        (key, Jsonb(payload)),
    )
    row = conn.execute(
        "SELECT request, response FROM schedule_operation_receipts WHERE operation_key = %s FOR UPDATE",
        (key,),
    ).fetchone()
    assert row is not None  # noqa: S101 — insert or conflicting committed receipt
    if row[0] != payload:
        raise HTTPException(
            status_code=409,
            detail="idempotency key was already used for a different schedule request",
        )
    return None if row[1] is None else tuple(row[1])


def finish(conn: Connection[Any], key: str | None, row: tuple[Any, ...]) -> None:
    if key is not None:
        values = [value.isoformat() if isinstance(value, datetime) else value for value in row]
        conn.execute(
            "UPDATE schedule_operation_receipts SET response = %s WHERE operation_key = %s",
            (Jsonb(values), key),
        )
