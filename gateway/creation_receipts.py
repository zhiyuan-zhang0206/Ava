"""Creation acceptance identities for presets and schedules, without request copies.

The caller owns the business transaction. Only a fingerprint and original
resource ID/timestamps survive: replay reconstructs the original view from the
matching caller request, avoiding an extra archive of opaque configuration.
"""

import hashlib
import json
from datetime import datetime
from typing import Any, NamedTuple

from fastapi import HTTPException, Request
from psycopg import Connection

from gateway.auth.request_principal import PrincipalScopeError, optional_request_key


class Creation(NamedTuple):
    resource_id: int
    created_at: datetime
    updated_at: datetime


def operation_key(request: Request) -> str | None:
    try:
        key = optional_request_key(request)
    except PrincipalScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return None if key is None else f"{request.method}:{request.url.path}:{key}"


def begin(conn: Connection[Any], key: str | None, payload: dict[str, Any]) -> Creation | None:
    if key is None:
        return None
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    conn.execute(
        "INSERT INTO resource_creation_receipts (operation_key, request_fingerprint) VALUES (%s, %s) "
        "ON CONFLICT DO NOTHING",
        (key, fingerprint),
    )
    row = conn.execute(
        "SELECT request_fingerprint, resource_id, resource_created_at, resource_updated_at "
        "FROM resource_creation_receipts WHERE operation_key = %s FOR UPDATE",
        (key,),
    ).fetchone()
    assert row is not None  # noqa: S101 — insert or conflicting committed receipt
    if row[0] != fingerprint:
        raise HTTPException(
            status_code=409,
            detail="idempotency key was already used for a different creation request",
        )
    return None if row[1] is None else Creation(row[1], row[2], row[3])


def finish(conn: Connection[Any], key: str | None, created: Creation) -> None:
    if key is not None:
        conn.execute(
            "UPDATE resource_creation_receipts SET resource_id = %s, resource_created_at = %s, "
            "resource_updated_at = %s WHERE operation_key = %s",
            (*created, key),
        )
