"""Strict runtime evidence value types.

Health endpoints share these without importing rollout leases, database fences
or retired updater orchestration. Nothing here grants startup authority.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ExpectedProcess(EvidenceModel):
    pid: int = Field(gt=0)
    create_time: float = Field(gt=0, allow_inf_nan=False)
    starttime: int | None = Field(default=None, gt=0)
