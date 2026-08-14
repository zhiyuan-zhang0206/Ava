"""user settings.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from pydantic import (
    BaseModel,
    ConfigDict,
)


class UserSettingRow(BaseModel):
    """One row from the user_settings table (key-value store for frontend preferences)."""

    model_config = ConfigDict(frozen=True)

    key: str
    value: object  # JSONB — opaque to the gateway
    updated_at: str  # ISO-8601


class UserSettingListResponse(BaseModel):
    """GET /api/settings response — every user_settings row."""

    model_config = ConfigDict(frozen=True)

    settings: list[UserSettingRow]


class UserSettingUpdateRequest(BaseModel):
    """PUT /api/settings/{key} request body."""

    value: object  # JSONB — opaque to the gateway
