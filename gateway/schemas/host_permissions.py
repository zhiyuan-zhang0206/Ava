from __future__ import annotations

from pydantic import BaseModel, ConfigDict


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
