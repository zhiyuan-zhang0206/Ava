"""User settings endpoints — persistent key-value store for frontend preferences.

GET /api/settings — list all settings.
PUT /api/settings/{key} — upsert a single setting.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from psycopg.types.json import Jsonb

from gateway.schemas import UserSettingListResponse, UserSettingRow, UserSettingUpdateRequest
from shared.db_transaction import write_transaction

router = APIRouter()


@router.get("/api/settings")
def get_settings(request: Request) -> UserSettingListResponse:
    """Return every row in the user_settings table."""
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT key, value, updated_at FROM user_settings ORDER BY key")
        settings = [
            UserSettingRow(
                key=r[0],
                value=r[1],
                updated_at=r[2].isoformat(),
            )
            for r in cur.fetchall()
        ]
    return UserSettingListResponse(settings=settings)


@router.put("/api/settings/{key}")
def put_setting(key: str, body: UserSettingUpdateRequest, request: Request) -> UserSettingRow:
    """Upsert a single setting by key."""
    with write_transaction(request.app.state.db_pool) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_settings (key, value, updated_at) "
            "VALUES (%s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now() "
            "RETURNING key, value, updated_at",
            (key, Jsonb(body.value)),
        )
        row = cur.fetchone()
        assert row is not None  # noqa: S101 — upsert RETURNING always yields a row
    return UserSettingRow(
        key=row[0],
        value=row[1],
        updated_at=row[2].isoformat(),
    )
