from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PermissionItemRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    group: str
    state: str
    state_label: str
    mode: str
    why: str
    jump: str
    verify: str
    restart: str
    required: bool
    blocking: bool
    detail: str = ""


class HostPermissionsPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: str
    headline: str
    checked_at: str
    diagnostic: str = ""
    helper_reachable: bool
    required_total: int
    required_missing: int
    items: list[PermissionItemRow]


class HostPermissionsRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    confirm_user_present: bool = Field(
        description=(
            "The caller attests a person is at the machine. Required: a pending"
            " permission dialog blocks synthesized input machine-wide until answered."
        )
    )
