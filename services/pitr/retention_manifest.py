"""Canonical, local-only evidence for a PITR retention dry run."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

PLAN_SCHEMA_VERSION = 1


@dataclass(frozen=True, order=True)
class RetentionObject:
    object_name: str
    generation: int
    size: int
    archive_name: str | None
    kind: str

    def __post_init__(self) -> None:
        if not self.object_name or self.generation <= 0 or self.size <= 0:
            raise ValueError("retention object lacks an exact immutable identity")
        if self.kind not in {"base", "wal", "history"}:
            raise ValueError("retention object kind is unsupported")


@dataclass(frozen=True)
class RetentionDecision:
    object: RetentionObject
    reason: str


@dataclass(frozen=True)
class RetentionPlan:
    schema_version: int
    retained_chain_count: int
    evidence_sha256: str
    protected_chain_ids: tuple[str, ...]
    unprotected_chain_ids: tuple[str, ...]
    oldest_retained_chain_id: str | None
    ack_high_water: str | None
    blocked_reasons: tuple[str, ...]
    retained: tuple[RetentionDecision, ...]
    eligible: tuple[RetentionDecision, ...]
    retained_bytes: int
    eligible_bytes: int

    def __post_init__(self) -> None:
        if self.schema_version != PLAN_SCHEMA_VERSION or self.retained_chain_count < 2:
            raise ValueError("retention plan schema or chain count is invalid")
        if tuple(sorted(set(self.blocked_reasons))) != self.blocked_reasons:
            raise ValueError("retention plan blockers must be canonical")
        if self.blocked_reasons and self.eligible:
            raise ValueError("a blocked retention plan cannot contain eligible objects")
        if self.retained_bytes != sum(item.object.size for item in self.retained):
            raise ValueError("retained byte total differs from its decisions")
        if self.eligible_bytes != sum(item.object.size for item in self.eligible):
            raise ValueError("eligible byte total differs from its decisions")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    @classmethod
    def from_json(cls, value: str) -> RetentionPlan:
        raw: dict[str, Any] = json.loads(value)
        if set(raw) != set(cls.__dataclass_fields__):
            raise ValueError("retention plan fields do not match schema")
        raw["protected_chain_ids"] = tuple(raw["protected_chain_ids"])
        raw["unprotected_chain_ids"] = tuple(raw["unprotected_chain_ids"])
        raw["blocked_reasons"] = tuple(raw["blocked_reasons"])
        raw["retained"] = tuple(_decision(item) for item in raw["retained"])
        raw["eligible"] = tuple(_decision(item) for item in raw["eligible"])
        return cls(**raw)


def _decision(raw: dict[str, Any]) -> RetentionDecision:
    if set(raw) != {"object", "reason"}:
        raise ValueError("retention decision fields do not match schema")
    return RetentionDecision(RetentionObject(**raw["object"]), str(raw["reason"]))
